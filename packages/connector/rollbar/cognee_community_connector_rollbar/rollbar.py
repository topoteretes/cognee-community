"""Rollbar connector for cognee: a dlt resource that syncs a project's error items to memory.

Each Rollbar item, the group Rollbar keeps for every occurrence of one error, becomes one
document: its title, environment, level, resolution state and an excerpt of the occurrence
that activated it. Occurrences are never ingested one by one::

    await cognee.remember(
        rollbar_source(123456, access_token="<project read token>"),
        dataset_name="rollbar",
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
        self_improvement=False,
    )

Design
------
* **Grouped by item.** The document carries no exact occurrence count, last-seen time or
  occurrence id. They change with every new occurrence, so rendering them would
  re-cognify every active item on every sync. Volume is rendered as an order of
  magnitude instead, so a document changes when an item crosses 10, 100, 1,000, ...
* **One excerpt per item**, from ``activating_occurrence_id``: the first occurrence since
  the item was last resolved, so it only changes when the item regresses. Only the
  exception class and message, the innermost frames and ``code_version`` are kept; the
  request, person, local variables, arguments and custom data never leave Rollbar.
* **Incremental.** ``GET /api/1/items`` has no time filter and items carry no modified
  time, so every run lists the selected items in full and emits only the documents whose
  rendered text changed, using a fingerprint kept in the dlt resource state. Resolving,
  muting or regressing an item changes its document; another occurrence does not.
* **Forget-on-delete mirrors what the API serves.** Rollbar has no item delete: deleting
  every occurrence of an item drops it from the listing, and Rollbar's docs say merging it
  into another item does too. An item the listing no longer holds is forgotten. An excerpt whose
  occurrence is gone (deleted, or expired by the plan's retention) returns 404 and is
  removed from its document on the next run, so every run reads each item's occurrence.
* **Paging.** Pages are numbered while items keep changing, so an item can slip between
  two pages. Rollbar ignores the ``ids`` filter, so a run that misses known items lists
  them all a second time and forgets only the items absent from both listings.
* **Never sleeps on the rate limit.** A 429 ends the run cleanly: documents emitted so
  far are kept, nothing is forgotten and the next run resumes.
"""

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, NODE_SET_COLUMN

logger = logging.getLogger(__name__)

API_URL = "https://api.rollbar.com/api/1"
STATUSES = ("active", "resolved", "muted", "archived")
# the statuses the listing returns when no status filter is sent (checked live)
DEFAULT_STATUSES = ("active", "resolved", "muted")
LEVELS = ("debug", "info", "warning", "error", "critical")
_TIMEOUT = 30.0
_RETRIES = 3
_FRAMES = 5
_CHAIN = 3
_TEXT_LIMIT = 500


class RollbarSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class RollbarAPIError(RollbarSourceError):
    """Rollbar answered with an error or an unexpected shape, or could not be reached."""


class RollbarAuthError(RollbarSourceError):
    """Rollbar refused the access token, its scope or its access to the project."""


class RollbarRateLimitedError(RollbarSourceError):
    """Rollbar answered 429: the token's request limit is used up for now."""


class _NotFoundError(RollbarAPIError):
    pass


class _CutShortError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return _text(body.get("message")) if isinstance(body, dict) else ""


class RollbarClient:
    """The read endpoints this connector uses, with retries on server and network errors."""

    def __init__(
        self,
        http: httpx.Client,
        access_token: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.http = http
        self.headers = {"X-Rollbar-Access-Token": access_token, "Accept": "application/json"}
        self.sleep = sleep

    def _get(self, path: str, params: list[tuple[str, Any]] | None = None) -> Any:
        attempt = 0
        while True:
            try:
                response = self.http.get(f"{API_URL}{path}", params=params, headers=self.headers)
            except httpx.TransportError as exc:
                if attempt == _RETRIES:
                    raise RollbarAPIError(
                        f"could not reach Rollbar ({type(exc).__name__})"
                    ) from None
            else:
                if response.status_code < 500 or attempt == _RETRIES:
                    return self._result(response, path)
            self.sleep(2**attempt)
            attempt += 1

    @staticmethod
    def _result(response: httpx.Response, path: str) -> Any:
        status = response.status_code
        if status == 429:
            raise RollbarRateLimitedError("Rollbar rate limit reached (HTTP 429)")
        if status in (401, 403):
            detail = _message(response) or "access denied"
            raise RollbarAuthError(
                f"Rollbar refused the request: {detail} (HTTP {status}). Use a project access "
                "token with read scope, or an account token that can read the project."
            )
        if status == 404:
            raise _NotFoundError(f"Rollbar has no {path} (HTTP 404)")
        if status != 200:
            raise RollbarAPIError(f"Rollbar answered {path} with HTTP {status}")
        try:
            body = response.json()
        except ValueError:
            raise RollbarAPIError(f"Rollbar answered {path} with a body that is not JSON") from None
        if not isinstance(body, dict) or body.get("err") != 0 or "result" not in body:
            raise RollbarAPIError(f"Rollbar answered {path} with an unexpected shape")
        return body["result"]

    def list_items(
        self,
        project_id: int,
        *,
        statuses: tuple[str, ...],
        environments: tuple[str, ...] = (),
        levels: tuple[str, ...] = (),
    ) -> list[dict]:
        """Return every item the filters select, following pages until an empty one."""
        # a project token ignores project_ids; an account token needs it
        params: list[tuple[str, Any]] = [("project_ids", project_id)]
        params += [("status", status) for status in statuses]
        params += [("environment", environment) for environment in environments]
        params += [("level", level) for level in levels]
        items: dict[str, dict] = {}
        page = 1
        while True:
            result = self._get("/items", [*params, ("page", page)])
            batch = result.get("items") if isinstance(result, dict) else None
            if not isinstance(batch, list) or not all(
                isinstance(item, dict) and _text(item.get("id")) for item in batch
            ):
                raise RollbarAPIError("Rollbar answered /items with an unexpected shape")
            if not batch:
                return list(items.values())
            fresh = 0
            for item in batch:
                fresh += _text(item["id"]) not in items
                items[_text(item["id"])] = item
            # a page that adds nothing means paging is not advancing
            if not fresh:
                raise RollbarAPIError(f"Rollbar returned page {page} of /items again")
            page += 1

    def occurrence(self, occurrence_id: Any) -> dict | None:
        """Return an occurrence, or None once it was deleted or expired."""
        try:
            result = self._get(f"/instance/{quote(_text(occurrence_id), safe='')}")
        except _NotFoundError:
            return None
        if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
            raise RollbarAPIError("Rollbar answered /instance with an unexpected shape")
        return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _clip(value: Any) -> str:
    text = " ".join(_text(value).split())
    return text if len(text) <= _TEXT_LIMIT else text[: _TEXT_LIMIT - 1] + "…"


def _date(timestamp: Any) -> str | None:
    if not isinstance(timestamp, int | float) or isinstance(timestamp, bool) or timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, UTC).date().isoformat()


def _volume(count: Any) -> str | None:
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return None
    if count < 10:
        return "fewer than 10"
    return f"{10 ** (len(str(count)) - 1):,}+"


def _frame(frame: dict) -> list[str]:
    where = _text(frame.get("filename")) or "<unknown file>"
    if frame.get("lineno") is not None:
        where += f":{frame['lineno']}"
    if frame.get("method"):
        where += f" in {frame['method']}"
    code = _clip(frame.get("code"))
    return [f"  {where}", f"    {code}"] if code else [f"  {where}"]


def _trace(trace: Any, label: str) -> list[str]:
    if not isinstance(trace, dict):
        return []
    exception = trace.get("exception") if isinstance(trace.get("exception"), dict) else {}
    name = _clip(exception.get("class")) or "Exception"
    message = _clip(exception.get("message"))
    lines = [f"{label}: {name}: {message}" if message else f"{label}: {name}"]
    frames = [frame for frame in trace.get("frames") or [] if isinstance(frame, dict)]
    # rollbar stores frames with the most recent call last
    if frames:
        lines.append("Stack (most recent call last):")
        for frame in frames[-_FRAMES:]:
            lines += _frame(frame)
    return lines


def render_excerpt(occurrence: dict) -> list[str]:
    """The lines kept from one occurrence: its exception or message, frames and code version."""
    data = occurrence.get("data") or {}
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    if "trace" in body:
        lines = _trace(body["trace"], "Exception")
    elif "trace_chain" in body:
        chain = body["trace_chain"] if isinstance(body["trace_chain"], list) else []
        lines = []
        for position, trace in enumerate(chain[:_CHAIN]):
            lines += _trace(trace, "Exception" if position == 0 else "Caused by")
    elif "message" in body:
        message = body["message"] if isinstance(body["message"], dict) else {}
        text = _clip(message.get("body"))
        lines = [f"Message: {text}"] if text else []
    elif "crash_report" in body:
        lines = ["Crash report (the raw report is not ingested)"]
    else:
        lines = []
    server = data.get("server") if isinstance(data.get("server"), dict) else {}
    code_version = _clip(data.get("code_version") or server.get("code_version"))
    if code_version:
        lines.append(f"Code version: {code_version}")
    return lines


def _known(value: Any) -> str:
    text = _clip(value)
    return "" if text.lower() in ("", "unknown") else text


def item_id(project_id: Any, item: dict) -> str:
    return f"projects/{project_id}/items/{item['id']}"


def render_item(item: dict, occurrence: dict | None, *, account_slug: str | None = None) -> dict:
    """Render one item, with its excerpt when the occurrence is still served, as a document row."""
    project_id = item.get("project_id")
    lines = [f"Rollbar item #{item.get('counter')}"]
    if item.get("project_slug"):
        lines[0] += f" in project {item['project_slug']}"
    fields = [
        ("Environment", _known(item.get("environment"))),
        ("Level", _known(item.get("level"))),
        ("Framework", _known(item.get("framework"))),
        ("Platform", _known(item.get("platform"))),
        ("Status", _known(item.get("status"))),
        ("First seen", _date(item.get("first_occurrence_timestamp"))),
        ("Last activated", _date(item.get("last_activated_timestamp"))),
        ("Last resolved", _date(item.get("last_resolved_timestamp"))),
        ("Resolved in version", _known(item.get("resolved_in_version"))),
        ("Assigned", "yes" if item.get("assigned_user_id") or item.get("assigned_team_id") else ""),
        ("Occurrences since last activated", _volume(item.get("total_occurrences"))),
    ]
    lines += [f"{label}: {value}" for label, value in fields if value]
    excerpt = render_excerpt(occurrence) if occurrence is not None else []
    if excerpt:
        lines += ["", *excerpt]
    url = None
    if account_slug and item.get("project_slug") and item.get("counter") is not None:
        url = (
            f"https://rollbar.com/{quote(account_slug, safe='')}/"
            f"{quote(_text(item['project_slug']), safe='')}/items/{item['counter']}/"
        )
    environment = _known(item.get("environment"))
    node_sets = [f"rollbar:{project_id}"]
    if environment:
        node_sets.append(f"rollbar:{project_id}:{environment}")
    return {
        "id": item_id(project_id, item),
        "title": _clip(item.get("title")) or f"Rollbar item #{item.get('counter')}",
        "content": "\n".join(lines),
        "url": url,
        "_deleted": False,
        NODE_SET_COLUMN: node_sets,
    }


def _hash(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
class _Run:
    """One sync of a project, writing fingerprints into ``state``."""

    def __init__(
        self,
        client: Any,
        project_id: int,
        state: dict,
        stats: dict[str, int],
        *,
        statuses: tuple[str, ...],
        environments: tuple[str, ...],
        levels: tuple[str, ...],
        account_slug: str | None,
    ):
        self.client = client
        self.project_id = project_id
        self.stats = stats
        for key in ("scanned", "skipped", "deleted", "failed", "excerpts_gone"):
            stats.setdefault(key, 0)
        self.filters = {"statuses": statuses, "environments": environments, "levels": levels}
        self.account_slug = account_slug
        self.emitted = 0
        stored = state.get("project_id")
        if stored is not None and stored != project_id:
            raise ValueError(
                f"this resource already holds items of Rollbar project {stored}; "
                "give each project its own resource_name"
            )
        state["project_id"] = project_id
        self.fingerprints: dict[str, str] = state.setdefault("fingerprints", {})

    def _call(self, method: Callable, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except RollbarRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except RollbarAuthError:
            if self.emitted:
                raise _CutShortError("access") from None
            raise

    def _listing(self) -> dict[str, dict]:
        items = self._call(self.client.list_items, self.project_id, **self.filters)
        for item in items:
            if item.get("project_id") != self.project_id:
                raise RollbarAPIError(
                    f"the token reads Rollbar project {item.get('project_id')}, not "
                    f"{self.project_id}; use that project's token or fix project_id"
                )
        return {item_id(self.project_id, item): item for item in items}

    def rows(self) -> Iterator[dict]:
        listed = self._listing()
        if set(self.fingerprints) - set(listed):
            # an item can slip between two numbered pages; only forget what both listings miss
            listed = {**self._listing(), **listed}
        for row_id in sorted(listed):
            item = listed[row_id]
            occurrence = None
            if item.get("activating_occurrence_id"):
                occurrence = self._call(self.client.occurrence, item["activating_occurrence_id"])
                if occurrence is None:
                    self.stats["excerpts_gone"] += 1
            row = render_item(item, occurrence, account_slug=self.account_slug)
            self.stats["scanned"] += 1
            fingerprint = _hash(row)
            if self.fingerprints.get(row_id) == fingerprint:
                self.stats["skipped"] += 1
                continue
            self.fingerprints[row_id] = fingerprint
            self.emitted += 1
            yield row
        for row_id in sorted(set(self.fingerprints) - set(listed)):
            del self.fingerprints[row_id]
            self.stats["deleted"] += 1
            yield {"id": row_id, "_deleted": True}


def _iter_rows(
    client: Any,
    project_id: int,
    state: dict,
    stats: dict[str, int],
    *,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    environments: tuple[str, ...] = (),
    levels: tuple[str, ...] = (),
    account_slug: str | None = None,
) -> Iterator[dict]:
    """Yield the changed rows of a project. Pure of dlt, so tests drive it with a dict."""
    run = _Run(
        client,
        project_id,
        state,
        stats,
        statuses=statuses,
        environments=environments,
        levels=levels,
        account_slug=account_slug,
    )
    try:
        yield from run.rows()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Rollbar sync stopped early (%s); it resumes on the next run.", cut.reason)


def _choice(name: str, values: Any, allowed: tuple[str, ...] | None) -> tuple[str, ...]:
    values = (values,) if isinstance(values, str) else tuple(values)
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError(f"{name} must be a list of non-empty strings")
    if allowed is not None and set(values) - set(allowed):
        raise ValueError(f"{name} must be a subset of {allowed}")
    return values


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def rollbar_source(
    project_id: int | str,
    *,
    access_token: str | None = None,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    environments: tuple[str, ...] = (),
    levels: tuple[str, ...] = (),
    account_slug: str | None = None,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    http_client: httpx.Client | None = None,
):
    """Return a ``dlt`` resource that yields a Rollbar project's items as documents.

    Args:
        project_id: The numeric project id (Project → Settings → General).
        access_token: A project access token with ``read`` scope, or an account token that
            can read the project. Falls back to ``ROLLBAR_ACCESS_TOKEN``.
        statuses: Which item statuses to sync, from ``"active"``, ``"resolved"``,
            ``"muted"`` and ``"archived"``. An item whose status leaves the selection is
            forgotten.
        environments: Only sync items of these environments; all when empty.
        levels: Only sync items of these levels (``"debug"`` ... ``"critical"``).
        account_slug: The account name in Rollbar URLs, used to link each document to its
            item. Rollbar's API does not expose it to a project token.
        resource_name: Stable name of this connection, ``rollbar_<project id>`` by default.
            It scopes the sync state and the cleanup of forgotten items, so keep it fixed.
        check_active: Optional host authorization checkpoint, called between rows.
        http_client: Pre-built ``httpx`` client, mainly an injection point for tests.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "Rollbar sync requires cognee>=1.6.4, which reads per-row node sets "
            "(DOCUMENT_SYNC_VERSION >= 2)."
        )
    project = _text(project_id).strip()
    # the id goes into document ids and node sets
    if not (project.isascii() and project.isdigit()):
        raise ValueError("project_id must be the numeric Rollbar project id")
    project_number = int(project)
    token = access_token or os.environ.get("ROLLBAR_ACCESS_TOKEN")
    if not token:
        raise ValueError("a Rollbar access token is required (or set ROLLBAR_ACCESS_TOKEN)")
    statuses = _choice("statuses", statuses, STATUSES)
    if not statuses:
        raise ValueError("statuses must name at least one status")
    environments = _choice("environments", environments, None)
    levels = _choice("levels", levels, LEVELS)
    resource_name = resource_name or f"rollbar_{project_number}"
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # cognee_node_set needs no json hint: ingest_dlt_source applies NODE_SET_COLUMN_HINT
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def rollbar_items():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0, excerpts_gone=0)
        owned = None if http_client is not None else httpx.Client(timeout=_TIMEOUT)
        try:
            client = RollbarClient(http_client or owned, token)
            rows = _iter_rows(
                client,
                project_number,
                dlt.current.resource_state(),
                stats,
                statuses=statuses,
                environments=environments,
                levels=levels,
                account_slug=account_slug,
            )
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if owned is not None:
                owned.close()

    resource = rollbar_items()
    # each row becomes a text document that goes through cognify
    setattr(resource, DOCUMENT_SOURCE_ATTR, "rollbar")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # counts only, never item titles or content
    resource.cognee_sync_stats = stats
    return resource
