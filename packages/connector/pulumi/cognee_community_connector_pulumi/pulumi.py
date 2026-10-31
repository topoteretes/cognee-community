"""Pulumi Cloud connector for cognee: a ``dlt`` source that turns stack history into memory.

Sync an organization's Pulumi stacks and their finished updates (``pulumi up``,
refresh, destroy, import) into cognee, incrementally and with
forget-on-delete. The resource is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_pulumi import pulumi_source

    await cognee.remember(
        pulumi_source("acme", token=token),
        dataset_name="pulumi",
        primary_key="id",
        write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )

Design
------
* **Rows** are flat ``{id, title, content, url, _deleted}``: one per stack
  (``stack:<id>``: organization, project, name, tags, resource count) and one per
  finished update (``update:<stack id>:<version>``: kind, result, times, message,
  resource changes, who ran it, the commit).
* **No secrets.** An update's ``config`` and ``environment`` and a stack's
  ``config`` can hold secret values, so they are never read; documents are built
  from an allowlist of fields, and every text field is scrubbed anyway.
* **Incremental.** A stack whose ``lastUpdate`` and resource count did not move is
  skipped. Update versions only increase, so each stack keeps the highest version
  it read as its cursor and only newer updates are fetched. An update still
  running is kept as pending and read again until it finishes, since ``lastUpdate``
  holds its start time and does not move when it ends.
* **Forget-on-delete.** A stack missing from a complete stack listing (removed,
  hidden from the token, or deselected) is emitted as a hard-delete tombstone
  together with its updates.
* **A run is bounded.** A long rate limit, a spent retry wait budget or a spent
  request budget stops the run cleanly, keeps the stacks it finished, and reports
  why in ``cognee_sync_stats``.

Privacy
-------
Stack and update history describe your infrastructure and who changed it. Nothing
is fetched until you construct a source and call ``remember``, and anyone with read
access to the target dataset can read what was ingested.
"""

import copy
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_pulumi.redact import scrub

logger = get_logger("pulumi_connector")

DEFAULT_API_URL = "https://api.pulumi.com"
DEFAULT_CONSOLE_URL = "https://app.pulumi.com"
# How far back the first sync of a stack reads its updates.
DEFAULT_SINCE_DAYS = 90
# Requests per run; a bigger organization finishes over several runs.
DEFAULT_MAX_REQUESTS = 1000
# Total time one run may wait on retries before it stops cleanly.
DEFAULT_MAX_SLEEP = 300.0
# Updates per page of a stack's history; the API defaults to 10.
_PAGE_SIZE = 100
# Pulumi Cloud usually answers in well under a second; only a stuck connection hits this.
_TIMEOUT = 30.0
# Attempts per request; backing off 1, 2, 4 and 8 seconds gives up after about 15.
_RETRIES = 5
# A longer Retry-After stops the run instead of waiting it out.
_MAX_RETRY_SLEEP = 60.0
# Waits are slept in steps this long, asking the host's check_active in between.
_WAIT_STEP = 1.0
# Tags can change without an update, and an update that saved no checkpoint leaves
# lastUpdate alone, so a stack's details and history are read again this often.
_DETAILS_INTERVAL = timedelta(days=1)
# The media type Pulumi's REST API documents for its requests.
_ACCEPT = "application/vnd.pulumi+8"
# An update with one of these results is finished; the others are still running.
_FINAL_RESULTS = {"succeeded", "failed"}
_STATS = (
    "scanned",
    "emitted",
    "unchanged",
    "deleted",
    "pending",
    "unreadable",
    "redacted",
    "failed",
    "failed_auth",
    "failed_budget",
    "failed_rate_limit",
)


# ---------------------------------------------------------------------------
# Errors: messages carry the HTTP status only, never the token or bodies.
# ---------------------------------------------------------------------------
class PulumiSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class PulumiAuthError(PulumiSourceError):
    """Pulumi Cloud rejected the access token (HTTP 401)."""


class PulumiRateLimitedError(PulumiSourceError):
    """Pulumi Cloud kept answering HTTP 429, asked for a long wait, or the wait budget is spent."""


class PulumiAPIError(PulumiSourceError):
    """Pulumi Cloud answered with an error, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _retry_after(response: Any) -> float | None:
    try:
        return max(float(response.headers.get("Retry-After")), 0.0)
    except (TypeError, ValueError):
        return None


class PulumiClient:
    """Minimal synchronous client for the Pulumi Cloud REST API.

    ``get`` returns the decoded JSON body. Tests and hosts can pass any object
    with the same method to :func:`pulumi_source` instead.
    """

    def __init__(
        self,
        token: str,
        api_url: str = DEFAULT_API_URL,
        *,
        http: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        max_sleep: float = DEFAULT_MAX_SLEEP,
        check_active: Callable[[], None] | None = None,
    ):
        token = (token or "").strip()
        # Reject early and without echoing the value: httpx would put an invalid
        # header value, token included, into its exception.
        if not token or not token.isascii() or not token.isprintable() or " " in token:
            raise ValueError("The Pulumi access token is empty or has an invalid format")
        self.api_url = api_url.rstrip("/")
        self._headers = {"Authorization": f"token {token}", "Accept": _ACCEPT}
        # An injected client belongs to the caller; one made here is closed by close().
        self._http = http
        self._owned = http is None
        self._sleep = sleep
        self._max_sleep = max_sleep
        self._slept = 0.0
        self._check_active = check_active

    def __repr__(self) -> str:
        return "PulumiClient(token=<redacted>)"

    def reset_sleep_budget(self) -> None:
        """Start a new run's retry wait budget."""
        self._slept = 0.0

    def close(self) -> None:
        """Close the HTTP client this object made; the next request opens a new one."""
        if self._owned and self._http is not None:
            self._http.close()
            self._http = None

    def _wait(self, delay: float) -> None:
        if self._slept + delay > self._max_sleep:
            raise PulumiRateLimitedError("Pulumi retries used up the wait budget of this run")
        self._slept += delay
        # Slept in short steps, asking the host in between, so revoked access stops
        # a long wait within a second instead of at its end.
        left = delay
        while True:
            if self._check_active is not None:
                self._check_active()
            if left <= 0:
                return
            step = min(_WAIT_STEP, left)
            self._sleep(step)
            left -= step

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        import httpx

        if self._http is None:
            self._http = httpx.Client(timeout=_TIMEOUT)
        attempt = 0
        while True:
            last = attempt == _RETRIES - 1
            try:
                response = self._http.get(self.api_url + path, params=params, headers=self._headers)
            except httpx.TransportError as exc:
                if last:
                    raise PulumiAPIError(f"Pulumi request failed: {type(exc).__name__}") from None
                self._wait(float(2**attempt))
                attempt += 1
                continue
            status = response.status_code
            if status == 429:
                wait = _retry_after(response)
                if last or (wait is not None and wait > _MAX_RETRY_SLEEP):
                    raise PulumiRateLimitedError("Pulumi rate limit exceeded (HTTP 429)")
                self._wait(wait if wait is not None else float(2**attempt))
            elif status >= 500 and not last:
                wait = _retry_after(response)
                self._wait(min(wait if wait is not None else float(2**attempt), _MAX_RETRY_SLEEP))
            elif status == 401:
                raise PulumiAuthError("Pulumi rejected the access token (HTTP 401)")
            elif status != 200:
                raise PulumiAPIError(f"Pulumi request failed: HTTP {status}", status=status)
            else:
                return response.json()
            attempt += 1


# ---------------------------------------------------------------------------
# Rendering: an allowlist of fields, deterministic, no volatile values.
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return str(value or "").strip()


def _when(epoch: Any) -> str:
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool) or epoch <= 0:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _path(stack: dict) -> str:
    return f"{stack['orgName']}/{stack['projectName']}/{stack['stackName']}"


def _stack_url(stack: dict, console_url: str) -> str:
    links = stack.get("links") if isinstance(stack.get("links"), dict) else {}
    if _text(links.get("self")):
        return _text(links["self"])
    parts = (stack["orgName"], stack["projectName"], stack["stackName"])
    return console_url.rstrip("/") + "/" + "/".join(quote(p, safe="") for p in parts)


def render_stack(stack: dict, tags: dict[str, Any], console_url: str) -> dict[str, Any]:
    """A stack's document. ``lastUpdate`` is left out: it moves on every update,
    which the update documents already cover."""
    fields = [
        ("Stack", _path(stack)),
        ("Organization", stack["orgName"]),
        ("Project", stack["projectName"]),
        ("Resources", stack.get("resourceCount")),
    ]
    parts = [f"{label}: {value}" for label, value in fields if value not in (None, "")]
    pairs = sorted((_text(k), _text(v)) for k, v in (tags or {}).items() if _text(k))
    if pairs:
        parts.extend(["", "Tags:", *(f"- {k}: {v}" if v else f"- {k}" for k, v in pairs)])
    return {
        "id": f"stack:{stack['id']}",
        "title": f"Pulumi stack {_path(stack)}",
        "content": "\n".join(parts),
        "url": _stack_url(stack, console_url),
        "_deleted": False,
    }


def _changes(info: dict) -> str:
    counts = info.get("resourceChanges") if isinstance(info.get("resourceChanges"), dict) else {}
    items = sorted(
        (str(op), n)
        for op, n in counts.items()
        if isinstance(n, int) and not isinstance(n, bool) and n > 0
    )
    return ", ".join(f"{op} {n}" for op, n in items)


def render_update(update: dict, stack: dict, console_url: str) -> dict[str, Any]:
    """An update's document, built only from fields that cannot hold secrets: never
    ``info.config``, ``info.environment``, ``requestedByToken`` or policy configs."""
    info = update.get("info") if isinstance(update.get("info"), dict) else {}
    version = update["version"]
    kind = _text(info.get("kind")) or "update"
    result = _text(info.get("result"))
    requested = update.get("requestedBy") if isinstance(update.get("requestedBy"), dict) else {}
    who = _text(requested.get("name"))
    login = _text(requested.get("githubLogin"))
    if login and login != who:
        who = f"{who} ({login})" if who else login
    commit = update.get("githubCommitInfo")
    commit = commit if isinstance(commit, dict) else {}
    fields = [
        ("Stack", _path(stack)),
        ("Kind", kind),
        ("Result", result),
        ("Version", version),
        ("Started", _when(info.get("startTime"))),
        ("Finished", _when(info.get("endTime"))),
        ("Resource changes", _changes(info)),
        ("Resources after", info.get("resourceCount")),
        ("Requested by", who),
    ]
    if _text(commit.get("sha")):
        slug = _text(commit.get("slug"))
        fields.append(("Commit", f"{slug}@{_text(commit['sha'])[:12]}" if slug else commit["sha"]))
        first_line = _text(commit.get("message")).splitlines()[:1]
        fields.append(("Commit message", first_line[0] if first_line else ""))
    parts = [f"{label}: {value}" for label, value in fields if value not in (None, "")]
    message = _text(info.get("message"))
    if message:
        parts.extend(["", message])
    return {
        "id": f"update:{stack['id']}:{version}",
        "title": f"{kind} {result} on {_path(stack)} (version {version})".replace("  ", " "),
        "content": "\n".join(parts),
        "url": f"{_stack_url(stack, console_url)}/updates/{version}",
        "_deleted": False,
    }


def _scrubbed(row: dict[str, Any], stats: dict[str, int]) -> dict[str, Any]:
    for key in ("title", "content"):
        row[key], found = scrub(row[key])
        stats["redacted"] += found
    return row


def _digest(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _PulumiConfig:
    organization: str
    projects: tuple[str, ...] = ()
    tag_name: str | None = None
    tag_value: str | None = None
    since_days: int = DEFAULT_SINCE_DAYS
    full_resync: bool = False
    console_url: str = DEFAULT_CONSOLE_URL
    max_requests: int = DEFAULT_MAX_REQUESTS


class _CutShortError(Exception):
    """Internal: stop the run here, keeping the stacks finished so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _forget(entry: dict, stack_id: str, stats: dict[str, int]) -> Iterator[dict]:
    """Tombstones of a stack and every update ingested from it."""
    for version in entry.get("versions") or []:
        stats["deleted"] += 1
        yield {"id": f"update:{stack_id}:{version}", "_deleted": True}
    if entry.get("doc"):
        stats["deleted"] += 1
        yield {"id": f"stack:{stack_id}", "_deleted": True}


class _Walker:
    """One run over the organization's stacks, writing progress into ``state``."""

    def __init__(
        self,
        client: Any,
        config: _PulumiConfig,
        state: dict,
        stats: dict[str, int],
        check_active: Callable[[], None] | None,
        now: datetime,
    ):
        self.client = client
        self.config = config
        self.state = state
        self.stats = stats
        self.check_active = check_active
        self.now = now
        self.known: dict[str, dict] = state.setdefault("stacks", {})
        self.requests = 0
        # Set once a stack is finished; the budget never stops a run before that.
        self.progressed = False

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.check_active is not None:
            self.check_active()
        if self.progressed and self.requests >= self.config.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return self.client.get(path, params)
        except PulumiRateLimitedError:
            raise _CutShortError("rate_limit") from None
        except PulumiAuthError:
            # Revoked during the run: keep what is done. On the first request it is
            # a wrong token, which raises.
            if self.progressed:
                raise _CutShortError("auth") from None
            raise

    @staticmethod
    def _stack_path(stack: dict) -> str:
        return "/api/stacks/" + "/".join(
            quote(stack[key], safe="") for key in ("orgName", "projectName", "stackName")
        )

    # -- the stack listing -----------------------------------------------------
    def _listing(self) -> dict[str, dict]:
        """Every selected stack, by id, from a complete listing."""
        config = self.config
        stacks: dict[str, dict] = {}
        for project in config.projects or (None,):
            params: dict[str, Any] = {"organization": config.organization}
            if project:
                params["project"] = project
            if config.tag_name:
                params["tagName"] = config.tag_name
                if config.tag_value is not None:
                    params["tagValue"] = config.tag_value
            token = None
            while True:
                page = self._get(
                    "/api/user/stacks",
                    {**params, **({"continuationToken": token} if token else {})},
                )
                items = page.get("stacks") if isinstance(page, dict) else None
                # A 200 without the list must not read as "every stack was removed".
                if not isinstance(items, list):
                    raise PulumiAPIError("Pulumi answered without a stack list")
                for stack in items:
                    if not isinstance(stack, dict) or not all(
                        isinstance(stack.get(k), str) and stack[k]
                        for k in ("orgName", "projectName", "stackName")
                    ):
                        # An entry that cannot be told apart leaves the listing incomplete.
                        raise PulumiAPIError("Pulumi listed a stack without its names")
                    # The listing filters by organization; stay inside it regardless.
                    if stack["orgName"].lower() != config.organization.lower():
                        continue
                    stack.setdefault("id", _path(stack))
                    stacks[str(stack["id"])] = stack
                next_token = page.get("continuationToken")
                if not next_token:
                    break
                if next_token == token:
                    raise PulumiAPIError("Pulumi pagination did not advance")
                token = next_token
        return stacks

    def _forget_unlisted(self, stacks: dict[str, dict]) -> Iterator[dict]:
        """Stacks that left the listing or the selection. Decided right after the
        listing, so a run cut short by the budget still forgets them."""
        gone = [stack_id for stack_id in self.known if stack_id not in stacks]
        if gone and not stacks and not self.state.get("empty_listing"):
            # A listing that is empty as a whole is confirmed by the next run.
            self.state["empty_listing"] = True
            logger.warning(
                "Pulumi listed no stacks although %d were synced before; nothing is "
                "forgotten until the next run confirms it.",
                len(gone),
            )
            return
        self.state.pop("empty_listing", None)
        for stack_id in gone:
            yield from _forget(self.known.pop(stack_id), stack_id, self.stats)

    # -- one stack -------------------------------------------------------------
    def _details(self, stack: dict) -> dict | None:
        """The stack's tags, or None when it is gone since the listing (the next
        listing then decides). Its ``config`` is never read."""
        try:
            body = self._get(self._stack_path(stack))
        except PulumiAPIError as exc:
            if exc.status != 404:
                raise
            logger.warning("Pulumi: stack %s answered 404, left unchanged.", _path(stack))
            self.stats["unreadable"] += 1
            return None
        tags = body.get("tags") if isinstance(body, dict) else None
        return tags if isinstance(tags, dict) else {}

    def _history(self, stack: dict, cursor: int, floor: float | None) -> list[dict]:
        """Updates newer than ``cursor`` (and started after ``floor``), newest first."""
        found: list[dict] = []
        page = 1
        while True:
            body = self._get(
                f"{self._stack_path(stack)}/updates",
                {"output-type": "service", "page": page, "pageSize": _PAGE_SIZE},
            )
            items = body.get("updates") if isinstance(body, dict) else None
            if not isinstance(items, list):
                raise PulumiAPIError("Pulumi answered an update history without its list")
            reached = False
            for update in items:
                if not isinstance(update, dict) or not isinstance(update.get("version"), int):
                    raise PulumiAPIError("Pulumi answered an update without its version")
                info = update.get("info") if isinstance(update.get("info"), dict) else {}
                start = info.get("startTime")
                if update["version"] <= cursor or (
                    floor is not None and isinstance(start, (int, float)) and start < floor
                ):
                    reached = True
                    continue
                found.append(update)
            if reached or len(items) < _PAGE_SIZE:
                return found
            page += 1

    def _read_update(self, stack: dict, version: int) -> dict | None:
        try:
            return self._get(f"{self._stack_path(stack)}/updates/{version}")
        except PulumiAPIError as exc:
            if exc.status != 404:
                raise
            return None

    def _sync_stack(self, stack: dict, entry: dict) -> list[dict]:
        rows: list[dict] = []
        renamed = entry.get("path") not in (None, _path(stack))
        moved = entry.get("last") != stack.get("lastUpdate") or entry.get("count") != stack.get(
            "resourceCount"
        )
        checked = entry.get("details_at")
        stale = not checked or self.now - datetime.fromisoformat(checked) >= _DETAILS_INTERVAL
        new = not entry
        if not (
            new or renamed or moved or stale or entry.get("pending") or self.config.full_resync
        ):
            self.stats["unchanged"] += 1
            return rows

        tags = self._details(stack)
        if tags is None:
            return rows
        row = _scrubbed(render_stack(stack, tags, self.config.console_url), self.stats)
        if _digest(row) != entry.get("doc"):
            entry["doc"] = _digest(row)
            rows.append(row)
        entry["details_at"] = self.now.isoformat()

        versions: set[int] = set(entry.get("versions") or [])
        pending: set[int] = set(entry.get("pending") or [])
        # Versions the history just answered need no second read below.
        read_now: set[int] = set()
        if new or renamed or moved or stale or self.config.full_resync:
            # A renamed stack names itself in every update, so they are rendered again.
            reread = renamed or self.config.full_resync
            cursor = 0 if reread else int(entry.get("cursor") or 0)
            floor = None
            if new or reread:
                floor = (self.now - timedelta(days=self.config.since_days)).timestamp()
            for update in self._history(stack, cursor, floor):
                rows.extend(self._take(stack, update, versions, pending))
                read_now.add(update["version"])
                entry["cursor"] = max(int(entry.get("cursor") or 0), update["version"])
        for version in sorted(pending - read_now):
            if version in versions:
                pending.discard(version)
                continue
            update = self._read_update(stack, version)
            if update is None:
                pending.discard(version)
                continue
            rows.extend(self._take(stack, update, versions, pending))
        entry.update(
            path=_path(stack),
            last=stack.get("lastUpdate"),
            count=stack.get("resourceCount"),
            versions=sorted(versions),
            pending=sorted(pending),
        )
        return rows

    def _take(self, stack: dict, update: dict, versions: set, pending: set) -> list[dict]:
        info = update.get("info") if isinstance(update.get("info"), dict) else {}
        version = update["version"]
        if info.get("result") not in _FINAL_RESULTS:
            # Still running: read again on the next runs until it finishes.
            pending.add(version)
            self.stats["pending"] += 1
            return []
        pending.discard(version)
        versions.add(version)
        self.stats["emitted"] += 1
        return [_scrubbed(render_update(update, stack, self.config.console_url), self.stats)]

    # -- the run ---------------------------------------------------------------
    def rows(self) -> Iterator[dict]:
        stacks = self._listing()
        self.stats["scanned"] += len(stacks)
        yield from self._forget_unlisted(stacks)
        # Start after the stack a cut-short run reached, so every one gets its turn.
        order = sorted(stacks)
        resume = self.state.get("resume")
        if resume:
            order = [s for s in order if s > resume] + [s for s in order if s <= resume]
        for stack_id in order:
            # Work on a copy, so a stack that fails keeps its previous state.
            entry = copy.deepcopy(self.known.get(stack_id)) or {}
            rows = self._sync_stack(stacks[stack_id], entry)
            if entry:
                self.known[stack_id] = entry
            for row in rows:
                if row["id"].startswith("stack:"):
                    self.stats["emitted"] += 1
                yield row
            self.state["resume"] = stack_id
            self.progressed = True
        self.state.pop("resume", None)


def _iter_rows(
    client: Any,
    config: _PulumiConfig,
    state: dict,
    stats: dict[str, int],
    check_active: Callable[[], None] | None = None,
    now: datetime | None = None,
) -> Iterator[dict]:
    """Yield changed rows and tombstones. Pure of dlt, so tests drive it with a dict.

    A rejected token raises :class:`PulumiAuthError`, which is never read as "gone".
    """
    for key in _STATS:
        stats.setdefault(key, 0)
    reset = getattr(client, "reset_sleep_budget", None)
    if reset is not None:
        reset()
    walker = _Walker(client, config, state, stats, check_active, now or _utcnow())
    try:
        yield from walker.rows()
    except _CutShortError as cut:
        # Every cut leaves the sync incomplete, so failed is set; failed_<reason> says why.
        stats["failed"] = 1
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Pulumi sync stopped early (%s); it resumes on the next run.", cut.reason)
        return
    logger.info(
        "Pulumi: %d stack(s) listed, %d document(s) emitted, %d pending, %d deleted.",
        stats["scanned"],
        stats["emitted"],
        stats["pending"],
        stats["deleted"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
# dlt table names stay readable up to this length; the merged Asana connector uses it too.
_MAX_RESOURCE_NAME = 64


def _default_resource_name(config: _PulumiConfig) -> str:
    organization = config.organization
    slug = re.sub(r"[^a-z0-9]+", "_", organization.lower()).strip("_")
    if slug != organization:
        # Organization names differ in case and punctuation; the raw value keeps them
        # apart. A name with no ASCII letter or digit keeps only the hash.
        digest = hashlib.sha256(organization.encode()).hexdigest()[:8]
        slug = f"{slug}_{digest}" if slug else digest
    name = "pulumi_" + slug
    if config.projects or config.tag_name:
        # Two filtered syncs into one dataset must not share state and cleanup.
        scope = [sorted(config.projects), config.tag_name, config.tag_value]
        name += "_" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()[:8]
    if len(name) > _MAX_RESOURCE_NAME:
        digest = hashlib.sha256(name.encode()).hexdigest()[:8]
        name = name[: _MAX_RESOURCE_NAME - len(digest) - 1].rstrip("_") + "_" + digest
    return name


def _check_resource_name(name: Any) -> None:
    """A non-empty name of at most 64 characters that dlt keeps as it is.

    It names the table and scopes the state: an empty name would lose that scope,
    and two names dlt normalizes alike ("Foo-Bar", "foo_bar") would share a table.
    """
    from dlt.common.normalizers.naming.snake_case import NamingConvention

    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty string")
    if len(name) > _MAX_RESOURCE_NAME:
        raise ValueError(f"resource_name must be at most {_MAX_RESOURCE_NAME} characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")


def pulumi_source(
    organization: str,
    *,
    token: str | None = None,
    projects: list[str] | None = None,
    tag_name: str | None = None,
    tag_value: str | None = None,
    since_days: int = DEFAULT_SINCE_DAYS,
    full_resync: bool = False,
    api_url: str = DEFAULT_API_URL,
    console_url: str = DEFAULT_CONSOLE_URL,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    resource_name: str | None = None,
    check_active: Callable[[], None] | None = None,
    service: Any = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per Pulumi stack and update.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        organization: The Pulumi Cloud organization (for a personal account, the
            user name).
        token: A Pulumi Cloud access token (personal, team or organization).
        projects: Only stacks of these projects.
        tag_name: Only stacks carrying this tag, with ``tag_value`` if given.
        tag_value: The value ``tag_name`` must have.
        since_days: How far back the first sync of a stack reads its updates.
        full_resync: Render every stack and update again, within ``since_days``.
        api_url: API address, for a self-hosted Pulumi Cloud.
        console_url: Console address used for links, for a self-hosted Pulumi Cloud.
        max_requests: Request budget of one run, checked once a stack is finished. A
            bigger organization finishes over several runs; a spent budget sets
            ``failed`` and ``failed_budget``.
        resource_name: Staging table name, which scopes this sync's state and
            cleanup. Defaults to one derived from the organization (plus a hash of
            ``projects`` and the tag filter when set). Keep the same name to change
            the selection in place. A name you pass must be lowercase snake_case of
            at most 64 characters.
        check_active: Optional host authorization checkpoint, called before each
            request and around each row, and in the client built here during every
            retry wait.
        service: Pre-built client (see :class:`PulumiClient`). Mainly an injection
            point for tests and hosts. ``client`` is accepted as an alias.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError('The Pulumi connector requires dlt: pip install "cognee[dlt]".') from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Pulumi sync requires a cognee build with table-scoped DLT document cleanup."
        )
    if not isinstance(organization, str) or not organization.strip():
        raise ValueError("organization is required: pass the Pulumi organization name.")
    if isinstance(projects, str):
        raise TypeError("projects must be a list of project names, not a string")
    names = tuple(dict.fromkeys(str(p).strip() for p in projects or ()))
    if any(not name for name in names):
        raise ValueError("Pulumi project names must not be empty")
    if tag_value is not None and not tag_name:
        raise ValueError("tag_value needs tag_name")
    if isinstance(since_days, bool) or not isinstance(since_days, int) or since_days < 1:
        raise ValueError("since_days must be a whole number of at least 1")
    if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
        raise ValueError("max_requests must be a whole number of at least 1")

    config = _PulumiConfig(
        organization=organization.strip(),
        projects=names,
        tag_name=tag_name.strip() if tag_name else None,
        tag_value=tag_value,
        since_days=since_days,
        full_resync=bool(full_resync),
        console_url=console_url,
        max_requests=max_requests,
    )
    name = resource_name
    if name is None:
        name = _default_resource_name(config)
    _check_resource_name(name)
    service = service or client
    owned = service is None
    if owned:
        if not token:
            raise ValueError(
                "A Pulumi access token is required: pass token=, or a pre-built service."
            )
        service = PulumiClient(token, api_url, check_active=check_active)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def pulumi_documents():
        stats.clear()
        state = dlt.current.resource_state()
        rows = _iter_rows(service, config, state, stats, check_active)
        try:
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            # Also on errors and when dlt stops early; an injected service is the caller's.
            if owned:
                service.close()

    resource = pulumi_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "pulumi")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    resource.cognee_sync_stats = stats
    return resource
