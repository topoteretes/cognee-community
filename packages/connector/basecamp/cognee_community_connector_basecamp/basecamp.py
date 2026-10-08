"""Basecamp connector for cognee: a ``dlt`` source that turns Basecamp projects into memory.

Syncs messages, to-dos, documents and comments from a Basecamp account into
cognee, incrementally and with forget-on-delete::

    import cognee
    from cognee_community_connector_basecamp import basecamp_source

    await cognee.remember(
        basecamp_source(account_id="1234567"),
        dataset_name="basecamp",
        write_disposition="merge",  # required: remember() defaults to "replace"
        max_rows_per_table=0,       # 0 = no row cap (the default reads only 50)
    )

Design
------
* **One endpoint.** Every type comes from ``GET /projects/recordings.json?type=...``.
  It returns completed to-dos too (checked on a live account), so finished work
  is covered without a second call.
* **One row per recording**, primary key ``"<type>:<id>"``, in cognee's document
  shape ``{id, title, content, url}``. HTML bodies are turned into plain text,
  comments carry their parent's type and title, to-dos carry list, done state
  and due date. The text is built deterministically because cognee derives row
  ids from a content hash.
* **Incremental cursor per type.** Listings are read with
  ``sort=updated_at&direction=desc`` and paging stops once items are older than
  the saved cursor (minus a small overlap). Trashing and archiving both bump
  ``updated_at``, so the same cursor picks those up. The first page's ``etag``
  is sent back as ``If-None-Match``; a 304 means nothing changed for that list.
* **Deletes.** Items in the ``status=trashed`` listing become ``_deleted``
  tombstones (``write_disposition="merge"`` + a ``hard_delete`` column). Items
  purged before a sync saw them (emptied trash, or the 25-day trash expiry)
  vanish from every listing, so a periodic **reconciliation sweep** lists all
  active and archived ids and tombstones known ids that are gone.
* **Archived items are kept.** Archiving is not deleting; the sweep counts
  archived items as present.
* **Failure safety.** Any listing error raises. dlt only commits resource state
  (cursors, known ids, etags) when the load succeeds, and cognee only runs its
  orphan cleanup after a successful add, so a failed run changes nothing.

Limitations
-----------
* Only active projects are listed (the API default). Archiving or trashing a
  whole project removes its items from memory on the next sweep.
* Card tables, schedules, chat, uploads and question answers are not synced.
"""

from __future__ import annotations

import html
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    NODE_SET_COLUMN_HINT,
)

logger = get_logger("basecamp_connector")

BASECAMP_SOURCE_NAME = "basecamp"
API_HOST = "https://3.basecampapi.com"
TOKEN_URL = "https://launchpad.37signals.com/authorization/token"

# Recording types the connector syncs, mapped to the label used in the text.
SUPPORTED_TYPES: dict[str, str] = {
    "Message": "Message",
    "Todo": "To-do",
    "Document": "Document",
    "Comment": "Comment",
}
DEFAULT_TYPES: tuple[str, ...] = tuple(SUPPORTED_TYPES)

MAX_ATTEMPTS = 5
# Re-read a little before the cursor so items updated at nearly the same moment
# as the last run are never skipped. Re-yielding them is harmless: same content,
# same row id, so cognee does not process them again.
CURSOR_OVERLAP = timedelta(minutes=5)
DEFAULT_FULL_SYNC_EVERY = 10


class BasecampError(RuntimeError):
    """Base class for Basecamp API errors raised by this connector."""


class BasecampAuthError(BasecampError):
    """The access token was rejected (401) and could not be refreshed."""


class BasecampAccountInactiveError(BasecampError):
    """404 with ``Reason: Account Inactive``: expired trial or suspended account."""


class BasecampNotFoundError(BasecampError):
    """404: the item was deleted or the token cannot see it. Never retried."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class BasecampClient:
    """Small Basecamp API client: auth headers, Link paging, retries, token refresh.

    ``http_client`` is an injection point for tests (``httpx.MockTransport``).
    The access token is never logged or put into an exception message.
    """

    def __init__(
        self,
        account_id: str,
        access_token: str | None,
        user_agent: str,
        *,
        refresh_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        http_client: httpx.Client | None = None,
        sleep=None,
    ):
        self.base_url = f"{API_HOST}/{account_id}"
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self.user_agent = user_agent
        self.http = http_client or httpx.Client(timeout=30.0)
        self._sleep = sleep or time.sleep
        self.last_etag: str | None = None

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if extra:
            headers.update(extra)
        return headers

    def _can_refresh(self) -> bool:
        return bool(self._refresh_token and self._client_id and self._client_secret)

    def refresh_access_token(self) -> None:
        """Swap the refresh token for a new access token (Launchpad OAuth)."""
        if not self._can_refresh():
            raise BasecampAuthError(
                "Basecamp: the access token is missing or expired, and no refresh token "
                "with client_id/client_secret was given to renew it."
            )
        resp = self.http.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"User-Agent": self.user_agent},
        )
        if resp.status_code != 200:
            raise BasecampAuthError(f"Basecamp: token refresh failed with HTTP {resp.status_code}.")
        token = resp.json().get("access_token")
        if not token:
            raise BasecampAuthError("Basecamp: token refresh returned no access_token.")
        self._access_token = token
        logger.info("Basecamp: access token refreshed.")

    def request(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        """GET ``url`` with retries on 429/5xx. Returns 2xx or 304 responses only."""
        if not self._access_token:
            self.refresh_access_token()

        refreshed = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            resp = self.http.get(url, headers=self._headers(headers))
            status = resp.status_code

            if status == 401 and not refreshed and self._can_refresh():
                self.refresh_access_token()
                refreshed = True
                continue
            if status == 401:
                raise BasecampAuthError(
                    "Basecamp: request was not authorized (401). Check the access token."
                )
            if status == 404:
                # httpx headers are case-insensitive.
                if resp.headers.get("reason", "").strip().lower() == "account inactive":
                    raise BasecampAccountInactiveError(
                        "Basecamp: this account is inactive (expired trial or suspended)."
                    )
                raise BasecampNotFoundError(f"Basecamp: not found: {_redact(url)}")
            if status == 429 or status >= 500:
                if attempt == MAX_ATTEMPTS:
                    break
                delay = _retry_delay(resp, attempt)
                logger.warning(
                    "Basecamp: HTTP %d, retrying in %.1fs (attempt %d/%d).",
                    status,
                    delay,
                    attempt,
                    MAX_ATTEMPTS,
                )
                self._sleep(delay)
                continue
            if status == 304 or 200 <= status < 300:
                return resp
            raise BasecampError(f"Basecamp: unexpected HTTP {status} for {_redact(url)}")

        raise BasecampError(f"Basecamp: gave up after {MAX_ATTEMPTS} attempts for {_redact(url)}")

    def iter_pages(
        self, path: str, params: dict[str, Any] | None = None, etag: str | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield items across pages, following only the ``Link: rel="next"`` header.

        If ``etag`` is given it is sent as ``If-None-Match`` on the first page; a
        304 yields nothing. The first page's etag is left in ``self.last_etag``.
        """
        url: httpx.URL | None = httpx.URL(self.base_url + path, params=params or {})
        first = True
        self.last_etag = None
        while url is not None:
            extra = {"If-None-Match": etag} if (first and etag) else None
            resp = self.request(str(url), headers=extra)
            if first:
                self.last_etag = resp.headers.get("etag")
                if resp.status_code == 304:
                    self.last_etag = etag
                    return
                first = False
            yield from resp.json() or []
            next_link = resp.links.get("next", {}).get("url")
            url = httpx.URL(next_link) if next_link else None

    def list_recordings(
        self,
        record_type: str,
        *,
        status: str,
        project_ids: tuple[str, ...] = (),
        etag: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {
            "type": record_type,
            "status": status,
            "sort": "updated_at",
            "direction": "desc",
        }
        if project_ids:
            params["bucket"] = ",".join(project_ids)
        yield from self.iter_pages("/projects/recordings.json", params, etag=etag)


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return float(min(2**attempt, 60))


def _redact(url: str) -> str:
    """Drop the query string from a URL before it goes into a message."""
    return url.split("?", 1)[0]


# ---------------------------------------------------------------------------
# Rendering (pure functions)
# ---------------------------------------------------------------------------
_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "tr", "figure", "figcaption",
}  # fmt: skip


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(value: str | None) -> str:
    """Turn Basecamp rich text (HTML) into readable plain text."""
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = html.unescape("".join(parser.parts))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _date(value: Any) -> str:
    return _clean(value)[:10]


def row_id(record_type: str, recording_id: Any) -> str:
    return f"{record_type.lower()}:{recording_id}"


def recording_to_row(rec: dict[str, Any]) -> dict[str, Any] | None:
    """Build a cognee document row from one recording, or None if it has no text.

    Only stable fields go into the row (no ``updated_at``), so an item whose
    visible content did not change keeps the same content hash.
    """
    record_type = rec.get("type") or ""
    if record_type not in SUPPORTED_TYPES:
        return None

    label = SUPPORTED_TYPES[record_type]
    project = _clean((rec.get("bucket") or {}).get("name"))
    author = _clean((rec.get("creator") or {}).get("name"))
    parent = rec.get("parent") or {}
    parent_type = SUPPORTED_TYPES.get(parent.get("type"), _clean(parent.get("type")))
    parent_title = _clean(parent.get("title"))

    if record_type == "Comment":
        title = f'Comment on {parent_type.lower()} "{parent_title}"' if parent_title else "Comment"
        body = html_to_text(rec.get("content"))
    elif record_type == "Todo":
        title = _clean(rec.get("title") or rec.get("content"))
        body = html_to_text(rec.get("description"))
    else:
        title = _clean(rec.get("title") or rec.get("subject"))
        body = html_to_text(rec.get("content"))

    if not title and not body:
        return None

    lines = [f"Basecamp {label}"]
    if project:
        lines.append(f"Project: {project}")
    if author:
        lines.append(f"Author: {author}")
    if rec.get("created_at"):
        lines.append(f"Created: {_date(rec['created_at'])}")
    if record_type == "Todo":
        if parent_title:
            lines.append(f"To-do list: {parent_title}")
        lines.append(f"Completed: {'yes' if rec.get('completed') else 'no'}")
        if rec.get("due_on"):
            lines.append(f"Due: {rec['due_on']}")
        assignees = sorted(_clean(a.get("name")) for a in rec.get("assignees") or [])
        if assignees:
            lines.append(f"Assigned to: {', '.join(assignees)}")
    if record_type == "Comment" and parent_title:
        lines.append(f'On {parent_type.lower()}: "{parent_title}"')
    if rec.get("status") == "archived":
        lines.append("Status: archived")

    content = "\n".join(lines)
    if body:
        content += "\n\n" + body

    row: dict[str, Any] = {
        "id": row_id(record_type, rec["id"]),
        "title": title or f"{label} {rec['id']}",
        "content": content,
        "url": rec.get("app_url"),
        "_deleted": False,
    }
    if project:
        row[NODE_SET_COLUMN] = [project]
    return row


def _tombstone(record_type: str, recording_id: Any) -> dict[str, Any]:
    return {"id": row_id(record_type, recording_id), "_deleted": True}


# ---------------------------------------------------------------------------
# Sync state machine (pure given a client + state dict, so it is unit-testable)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _SyncConfig:
    types: tuple[str, ...]
    project_ids: tuple[str, ...]
    full_sync: bool
    full_sync_every: int


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iter_rows(client: BasecampClient, config: _SyncConfig, state: dict) -> Iterator[dict]:
    """Yield changed rows and delete tombstones for every configured type.

    ``state`` is dlt's resource state (a plain dict in tests). It holds, per
    type: the ``updated_at`` cursor, the ids already synced, and list etags.
    """
    runs = int(state.get("runs", 0)) + 1
    sweep = (
        config.full_sync
        or "known_ids" not in state
        or (config.full_sync_every > 0 and runs % config.full_sync_every == 0)
    )
    cursors: dict[str, str] = state.setdefault("cursors", {})
    known_all: dict[str, list[str]] = state.setdefault("known_ids", {})
    etags: dict[str, str] = state.setdefault("etags", {})

    changed = deleted = 0
    for record_type in config.types:
        cursor = None if sweep else _parse_ts(cursors.get(record_type))
        stop_before = cursor - CURSOR_OVERLAP if cursor else None
        known = set(known_all.get(record_type, []))
        seen: set[str] = set()
        newest = cursor

        for status in ("active", "archived"):
            etag_key = f"{record_type}:{status}"
            etag = None if sweep else etags.get(etag_key)
            for rec in client.list_recordings(
                record_type, status=status, project_ids=config.project_ids, etag=etag
            ):
                updated = _parse_ts(rec.get("updated_at"))
                if stop_before and updated and updated < stop_before:
                    break
                newest = max(filter(None, (newest, updated)), default=None)
                rid = row_id(record_type, rec["id"])
                seen.add(rid)
                row = recording_to_row(rec)
                if row is None:
                    continue
                known.add(rid)
                changed += 1
                yield row
            # Any change bumps an item's updated_at and moves it to the top of the
            # newest-first listing, so an unchanged first page means no changes.
            if client.last_etag:
                etags[etag_key] = client.last_etag
            else:
                etags.pop(etag_key, None)

        for rec in client.list_recordings(
            record_type, status="trashed", project_ids=config.project_ids
        ):
            updated = _parse_ts(rec.get("updated_at"))
            if stop_before and updated and updated < stop_before:
                break
            newest = max(filter(None, (newest, updated)), default=None)
            rid = row_id(record_type, rec["id"])
            if rid in known:
                known.discard(rid)
                deleted += 1
                yield _tombstone(record_type, rec["id"])

        if sweep:
            # Anything we synced before that is neither active nor archived any
            # more was purged (emptied trash, 25-day expiry, project removed).
            for rid in sorted(known - seen):
                deleted += 1
                yield {"id": rid, "_deleted": True}
            known = {rid for rid in known if rid in seen}

        known_all[record_type] = sorted(known)
        if newest:
            cursors[record_type] = newest.isoformat()

    state["runs"] = runs
    logger.info(
        "Basecamp: %s sync yielded %d changed item(s) and %d deletion(s).",
        "full" if sweep else "incremental",
        changed,
        deleted,
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _split_ids(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        value = value.split(",")
    return tuple(str(v).strip() for v in value if str(v).strip())


def basecamp_source(
    account_id: str | int | None = None,
    *,
    project_ids: list[str] | tuple[str, ...] | str | None = None,
    types: list[str] | tuple[str, ...] | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    user_agent: str | None = None,
    full_sync: bool = False,
    full_sync_every: int | None = None,
    http_client: httpx.Client | None = None,
):
    """Return a ``dlt`` resource yielding one row per Basecamp recording.

    Arguments left as ``None`` fall back to ``BASECAMP_*`` environment
    variables. Hand the result to ``cognee.remember(...)`` with
    ``write_disposition="merge"`` and ``max_rows_per_table=0``.

    Args:
        account_id: Basecamp account id (the number in ``3.basecamp.com/<id>/``).
        project_ids: Limit the sync to these project ids. Default: all active projects.
        types: Recording types to sync. Default: Message, Todo, Document, Comment.
        access_token: OAuth access token (2-week lifetime).
        refresh_token, client_id, client_secret: Optional; used to renew the
            access token when it is missing, expired or rejected.
        user_agent: Required by Basecamp, e.g. ``"My App (me@example.com)"``.
        full_sync: Force a reconciliation sweep on this run.
        full_sync_every: Run the sweep every N runs (default 10; 0 disables).
        http_client: Pre-built ``httpx.Client``; mainly an injection point for tests.
    """
    try:
        import dlt
    except ImportError as exc:  # pragma: no cover - dlt ships with cognee
        raise ImportError(
            'The Basecamp connector requires dlt: pip install "cognee[dlt]".'
        ) from exc

    resolved_account = str(account_id or os.getenv("BASECAMP_ACCOUNT_ID") or "").strip()
    if not resolved_account:
        raise ValueError("account_id is required (pass it or set BASECAMP_ACCOUNT_ID).")

    resolved_agent = user_agent or os.getenv("BASECAMP_USER_AGENT")
    if not resolved_agent:
        raise ValueError(
            "user_agent is required by Basecamp, e.g. 'My Sync (me@example.com)' "
            "(pass it or set BASECAMP_USER_AGENT)."
        )

    resolved_types = tuple(types) if types else DEFAULT_TYPES
    unknown = [t for t in resolved_types if t not in SUPPORTED_TYPES]
    if unknown:
        raise ValueError(f"Unsupported Basecamp types: {unknown}. Use {list(SUPPORTED_TYPES)}.")

    every = (
        full_sync_every
        if full_sync_every is not None
        else int(os.getenv("BASECAMP_FULL_SYNC_EVERY", str(DEFAULT_FULL_SYNC_EVERY)))
    )
    config = _SyncConfig(
        types=resolved_types,
        project_ids=_split_ids(project_ids or os.getenv("BASECAMP_PROJECT_IDS")),
        full_sync=full_sync,
        full_sync_every=every,
    )

    token = access_token or os.getenv("BASECAMP_ACCESS_TOKEN")
    refresh = refresh_token or os.getenv("BASECAMP_REFRESH_TOKEN")
    cid = client_id or os.getenv("BASECAMP_CLIENT_ID")
    csecret = client_secret or os.getenv("BASECAMP_CLIENT_SECRET")
    if not token and not refresh:
        raise ValueError(
            "An access token is required (pass access_token or set BASECAMP_ACCESS_TOKEN), "
            "or a refresh_token with client_id and client_secret."
        )

    @dlt.resource(
        # One table per account, so syncing a second account never treats the
        # first account's rows as deleted.
        name=f"basecamp_{resolved_account}_recordings",
        write_disposition="merge",
        primary_key="id",
        # `_deleted` is a hard-delete marker: rows where it is True are removed
        # from the dlt destination on merge, and cognee's orphan cleanup then
        # forgets them. The node-set column stores the project name per row.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}, **NODE_SET_COLUMN_HINT},
    )
    def basecamp_recordings():
        client = BasecampClient(
            resolved_account,
            token,
            resolved_agent,
            refresh_token=refresh,
            client_id=cid,
            client_secret=csecret,
            http_client=http_client,
        )
        yield from _iter_rows(client, config, dlt.current.resource_state())

    resource = basecamp_recordings()
    # Opt into cognee's document path: each row becomes a text document that
    # goes through normal cognify. resolve_dlt_sources reads this marker.
    setattr(resource, DOCUMENT_SOURCE_ATTR, BASECAMP_SOURCE_NAME)
    return resource
