"""Twist connector for cognee: a ``dlt`` source that turns Twist threads into memory.

Syncs the threads of a Twist workspace (opening post plus every comment, in
order) and its channels into cognee, incrementally and with forget-on-delete::

    import cognee
    from cognee_community_connector_twist import twist_source

    await cognee.remember(
        twist_source("254427", access_token="..."),
        dataset_name="twist",
        write_disposition="merge",  # required: remember() defaults to "replace"
    )

Design
------
* **One document per thread**, primary key ``"thread:<id>"``: title, channel,
  author and date, the opening post, then every comment in order. A decision
  in a thread usually lives several comments down, so splitting comments into
  separate documents would cut each reply off from its context. Each channel
  is also a small ``"channel:<id>"`` document (name, description, archived).
* **Change signal.** Twist has no "updated since" listing that covers edits,
  so every run lists all threads of the selected channels (500 per request)
  and compares each thread's ``version``, ``last_updated_ts``,
  ``comment_count`` and ``channel_id`` with the previous run. Checked on a
  live workspace: ``version`` moves when the opening post is edited, the
  title changes, a comment is added or deleted, or the thread is moved or
  closed. ``last_updated_ts`` alone misses most of these. ``version`` also
  moves when a user bookmarks a thread, which the content hash absorbs.
* **Comment edits.** Editing the latest comment moves the thread's
  ``version``, but editing an earlier comment moves nothing on the thread, so
  a **sweep** re-reads every thread's comments on the first run and every Nth
  run (``full_sync_every``, default 10), or on demand with ``full_sync=True``.
  A content hash keeps unchanged threads from being sent to cognee again.
* **Deletes.** A thread or channel that is missing from a complete listing of
  the selected channels becomes a ``_deleted`` tombstone
  (``write_disposition="merge"`` + a ``hard_delete`` column), and cognee's
  orphan cleanup forgets it. Deleted comments disappear the next time their
  thread is rendered. A 404 is never treated as a delete on its own: a
  deleted thread answers 404 (checked live), and Twist's docs use the same
  error for an item the token can no longer see.
* **Failure safety.** Any API error aborts the run before a tombstone is
  written, and dlt commits the sync state only after a successful load. When a
  run finds more than half of what it knows gone, it holds those deletes back
  until the next run sees the same items gone (``allow_mass_delete=True``
  forgets them at once).

Traps found on a live workspace
-------------------------------
* A thread's ``is_archived`` is the user's own Inbox "done" state, not an
  archive, so it is ignored.
* Closed threads are hidden in the Twist UI but still returned by the API.
  They stay in memory, marked "closed".
* Archived channels are still returned by ``channels/get`` (with
  ``archived: true``). One that was never synced is skipped unless asked for;
  one that was synced before stays synced, since archiving deletes nothing.
* Title changes, moves and closes add system-message comments, and deleted
  comments stay in the list with ``deleted: true``. Neither is rendered.
* ``threads/get`` sorts by ``last_updated_ts`` until ``before_id`` is given and
  by id after that, so paging always sends ``before_id``.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    NODE_SET_COLUMN_HINT,
)

logger = get_logger("twist_connector")

TWIST_SOURCE_NAME = "twist"
API_BASE = "https://api.twist.com/api"
APP_BASE = "https://twist.com"

# Tries per request for 429, 5xx and network errors (waits 2, 4, 8 and 16s).
MAX_ATTEMPTS = 5
# A longer Retry-After stops the run instead of sleeping: cognee holds its dlt
# staging lock while a source runs, and the next sync simply retries.
MAX_RETRY_AFTER_SECONDS = 60.0
# threads/get and comments/get both reject a limit above 500 (error 20).
PAGE_LIMIT = 500
# The first threads/get page also sends before_id, so every page is sorted by id.
_FIRST_PAGE_BEFORE_ID = 2**53 - 1
DEFAULT_FULL_SYNC_EVERY = 10
# A run finding more than this share of the synced items gone, and at least
# MASS_DELETE_MIN_ITEMS of them, holds the deletes back for one run. Our own
# choice: small enough to catch a wrong workspace, channel list or token, large
# enough that ordinary cleanups never wait.
DEFAULT_MAX_DELETE_RATIO = 0.5
MASS_DELETE_MIN_ITEMS = 10

# Twist error_code for a rejected token (sent with HTTP 403, checked live).
_INVALID_TOKEN_CODE = 200

# Markdown pieces that are noise in memory: images, and links whose target is a
# Twist-internal or external URL. Only the link text is kept.
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)\s]+\)")
_BLANK_LINES = re.compile(r"\n{3,}")


class TwistError(RuntimeError):
    """Base class for Twist API errors raised by this connector."""


class TwistAuthError(TwistError):
    """The access token was rejected (HTTP 401, or 403 with error code 200)."""


class TwistForbiddenError(TwistError):
    """403: the token may not read this workspace, channel or thread."""


class TwistNotFoundError(TwistError):
    """404: the item was deleted or the token can no longer see it. Never retried."""


class TwistRateLimitError(TwistError):
    """429 that did not clear after retrying, or asked for too long a wait."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class TwistClient:
    """Small Twist API client: bearer auth, paging, retries.

    ``http_client`` is an injection point for tests (``httpx.MockTransport``).
    The access token is never logged or put into an exception message.
    """

    def __init__(
        self,
        access_token: str,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self.http = http_client or httpx.Client(timeout=30.0)
        self._sleep = sleep or time.sleep

    def request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{API_BASE}/{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.http.get(url, params=params, headers=self._headers)
            except httpx.TransportError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise TwistError(
                        f"Twist API unreachable on {path}: {type(exc).__name__}"
                    ) from None
                self._sleep(float(2**attempt))
                continue

            status = response.status_code
            if status == 429 or status >= 500:
                error = TwistRateLimitError if status == 429 else TwistError
                if attempt == MAX_ATTEMPTS:
                    raise error(f"Twist API returned {status} on {path} after {attempt} tries")
                delay = _retry_delay(response, attempt)
                if delay > MAX_RETRY_AFTER_SECONDS:
                    raise error(
                        f"Twist API returned {status} on {path} and asked to wait "
                        f"{delay:.0f}s; stopping so the next sync can retry."
                    )
                logger.warning(
                    "Twist: %s on %s, retrying in %.1fs (%d/%d).",
                    status,
                    path,
                    delay,
                    attempt,
                    MAX_ATTEMPTS,
                )
                self._sleep(delay)
                continue
            if status >= 400:
                raise _error_for(response, path)
            try:
                return response.json()
            except ValueError:
                raise TwistError(f"Twist API returned a body that is not JSON on {path}") from None

    def _list(self, path: str, params: dict[str, Any]) -> list[dict]:
        body = self.request(path, params)
        if not isinstance(body, list):
            # An odd 200 must never read as an empty listing: its missing items
            # would be forgotten.
            raise TwistError(
                f"Twist API returned an unexpected body on {path}; nothing was synced or forgotten."
            )
        return body

    def get_workspace(self, workspace_id: int) -> dict:
        body = self.request("v3/workspaces/getone", {"id": workspace_id})
        if not isinstance(body, dict):
            raise TwistError("Twist API returned an unexpected body on v3/workspaces/getone")
        return body

    def list_channels(self, workspace_id: int) -> list[dict]:
        """Active and archived channels, merged by id.

        Twist's docs say ``channels/get`` returns archived channels only with
        ``archived`` set; on a live workspace the plain call returned them too
        (flagged ``archived: true``), and ``archived=true`` returned only those.
        Asking for both is right either way.
        """
        channels: dict[int, dict] = {}
        for params in (
            {"workspace_id": workspace_id},
            {"workspace_id": workspace_id, "archived": "true"},
        ):
            for channel in self._list("v3/channels/get", params):
                channels[int(channel["id"])] = channel
        return list(channels.values())

    def iter_threads(self, channel_id: int) -> Iterator[dict]:
        """Every thread of a channel, closed ones included, newest id first."""
        before_id = _FIRST_PAGE_BEFORE_ID
        while True:
            page = self._list(
                "v3/threads/get",
                {"channel_id": channel_id, "limit": PAGE_LIMIT, "before_id": before_id},
            )
            yield from page
            if len(page) < PAGE_LIMIT:
                return
            next_before_id = min(thread["id"] for thread in page)
            if next_before_id >= before_id:
                # A page that does not move backwards would loop forever.
                raise TwistError(f"Twist paging did not advance in channel {channel_id}")
            before_id = next_before_id

    def iter_comments(self, thread_id: int, comment_count: int | None) -> Iterator[dict]:
        """Every comment of a thread in order, read in fixed obj_index windows.

        Without a range, ``limit=N`` returns the last N comments, so the windows
        are explicit. They cover at least the thread's ``comment_count`` and go on
        while a window comes back full. The thread's ``last_obj_index`` is not
        used: Twist's docs describe it as the reader's position ("-1 means the
        whole thread is unread"), not the thread's length.
        """
        if comment_count == 0:
            return
        start = 0
        while True:
            page = self._list(
                "v3/comments/get",
                {
                    "thread_id": thread_id,
                    "from_obj_index": start,
                    "to_obj_index": start + PAGE_LIMIT - 1,
                    "limit": PAGE_LIMIT,
                },
            )
            yield from page
            start += PAGE_LIMIT
            if len(page) < PAGE_LIMIT and (comment_count is None or start >= comment_count):
                return


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            pass
    return float(2**attempt)


def _error_for(response: httpx.Response, path: str) -> TwistError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    code = reason = None
    if isinstance(body, dict):
        # Live responses use error_code / error_string; Twist's docs also show
        # {"error": [code, string]}. Both are read.
        code, reason = body.get("error_code"), body.get("error_string")
        documented = body.get("error")
        if code is None and isinstance(documented, list) and documented:
            code = documented[0]
            reason = documented[1] if len(documented) > 1 else None
    detail = f"Twist API returned {response.status_code} on {path}"
    if code is not None:
        detail += f" (error {code}: {reason})"
    if response.status_code == 401 or code == _INVALID_TOKEN_CODE:
        return TwistAuthError(detail + ". Check the access token.")
    if response.status_code == 403:
        return TwistForbiddenError(detail)
    if response.status_code == 404:
        return TwistNotFoundError(detail)
    return TwistError(detail)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def clean_markdown(text: str | None) -> str:
    """Twist Markdown to plain-ish text: drop images, keep link text, trim."""
    if not text:
        return ""
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _date(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return ""


def _attachment_names(item: dict) -> list[str]:
    """Attachment names with their link, sorted so their API order does not
    change the text. The file itself is never downloaded."""
    names = []
    for attachment in item.get("attachments") or []:
        if isinstance(attachment, dict):
            name = attachment.get("title") or attachment.get("file_name")
            if name:
                url = attachment.get("url")
                names.append(f"{name} ({url})" if url else str(name))
    return sorted(names)


def _is_rendered_comment(comment: dict) -> bool:
    return not comment.get("deleted") and not comment.get("system_message")


def thread_url(workspace_id: int, channel_id: int, thread_id: int) -> str:
    return f"{APP_BASE}/a/{workspace_id}/ch/{channel_id}/t/{thread_id}/"


def channel_url(workspace_id: int, channel_id: int) -> str:
    return f"{APP_BASE}/a/{workspace_id}/ch/{channel_id}/"


def node_set(workspace_id: int, channel_id: int) -> list[str]:
    return [f"{TWIST_SOURCE_NAME}:{workspace_id}:{channel_id}"]


def render_thread(
    thread: dict, comments: list[dict], channel: dict, workspace_name: str = ""
) -> str:
    """Thread text for cognee. Deterministic: same input, same text."""
    title = (thread.get("title") or "").strip() or "(untitled)"
    lines = [f"Twist thread: {title}"]
    where = f"#{channel.get('name', '')}"
    if workspace_name:
        where += f" in {workspace_name}"
    lines.append(f"Channel: {where}")
    started = f"Started by {thread.get('creator_name') or 'unknown'}"
    if _date(thread.get("posted_ts")):
        started += f" on {_date(thread.get('posted_ts'))}"
    lines.append(started)
    if thread.get("closed"):
        lines.append("Status: closed")

    body = clean_markdown(thread.get("content"))
    if body:
        lines += ["", body]
    for name in _attachment_names(thread):
        lines.append(f"Attachment: {name}")

    for comment in sorted(comments, key=lambda c: c.get("obj_index", 0)):
        if not _is_rendered_comment(comment):
            continue
        text = clean_markdown(comment.get("content"))
        attachments = _attachment_names(comment)
        if not text and not attachments:
            continue
        header = f"{comment.get('creator_name') or 'unknown'}"
        if _date(comment.get("posted_ts")):
            header += f" on {_date(comment.get('posted_ts'))}"
        lines += ["", f"{header}:"]
        if text:
            lines.append(text)
        lines += [f"Attachment: {name}" for name in attachments]
    return "\n".join(lines)


def render_channel(channel: dict, workspace_name: str = "") -> str:
    lines = [f"Twist channel: #{channel.get('name', '')}"]
    if workspace_name:
        lines.append(f"Workspace: {workspace_name}")
    description = clean_markdown(channel.get("description"))
    if description:
        lines.append(f"Description: {description}")
    lines.append("Visibility: " + ("public" if channel.get("public") else "private"))
    if channel.get("archived"):
        lines.append("Status: archived")
    return "\n".join(lines)


def _digest(row: dict) -> str:
    payload = "\x1f".join(
        [row["title"], row["content"], row["url"], ",".join(row[NODE_SET_COLUMN])]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def _reset_stats(stats: dict[str, int]) -> None:
    stats.clear()
    stats.update(
        scanned=0,
        changed=0,
        unchanged=0,
        deleted=0,
        skipped=0,
        held_back=0,
        failed=0,
        failed_auth=0,
        failed_rate_limit=0,
    )


def _iter_rows(
    client: TwistClient,
    *,
    workspace_id: int,
    state: dict,
    stats: dict[str, int],
    channel_ids: list[int] | None = None,
    include_archived: bool = False,
    full_sync: bool = False,
    full_sync_every: int = DEFAULT_FULL_SYNC_EVERY,
    max_delete_ratio: float = DEFAULT_MAX_DELETE_RATIO,
    allow_mass_delete: bool = False,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield changed thread and channel rows, then tombstones for what vanished.

    ``state["items"]`` maps each row id to its fingerprint from the last run:
    ``[version, last_updated_ts, comment_count, channel_id, digest]`` for a
    thread, ``["channel", digest]`` for a channel. Any API error propagates and
    aborts the run before a tombstone is yielded; dlt then keeps the old state.
    """
    _reset_stats(stats)
    previous: dict[str, list] = dict(state.get("items") or {})
    runs = int(state.get("runs", 0))
    sweep = full_sync or runs == 0 or (full_sync_every > 0 and runs % full_sync_every == 0)

    workspace_name = client.get_workspace(workspace_id).get("name") or ""
    channels = client.list_channels(workspace_id)
    by_id = {int(channel["id"]): channel for channel in channels}
    if channel_ids:
        missing = [cid for cid in channel_ids if cid not in by_id]
        if missing:
            # A selected channel this token cannot see is a configuration or
            # permission problem, never proof that its threads were deleted.
            raise TwistNotFoundError(
                f"Twist channel(s) {missing} are not visible to this token in workspace "
                f"{workspace_id}. Nothing was synced or forgotten."
            )
        selected = [by_id[cid] for cid in channel_ids]
    else:
        # Archived channels are opt-in, but archiving deletes nothing: a channel
        # that was synced before stays synced, and its document says "archived".
        selected = [
            c
            for c in channels
            if include_archived or not c.get("archived") or f"channel:{c['id']}" in previous
        ]

    present: dict[str, list] = {}
    for channel in selected:
        if check_active is not None:
            check_active()
        channel_id = int(channel["id"])
        row = {
            "id": f"channel:{channel_id}",
            "title": f"#{channel.get('name', '')}",
            "content": render_channel(channel, workspace_name),
            "url": channel_url(workspace_id, channel_id),
            "_deleted": False,
            NODE_SET_COLUMN: node_set(workspace_id, channel_id),
        }
        fingerprint = ["channel", _digest(row)]
        present[row["id"]] = fingerprint
        if previous.get(row["id"]) != fingerprint:
            yield row

        for thread in client.iter_threads(channel_id):
            stats["scanned"] += 1
            key = f"thread:{thread['id']}"
            signal = [
                thread.get("version"),
                thread.get("last_updated_ts"),
                thread.get("comment_count"),
                thread.get("channel_id"),
            ]
            old = previous.get(key)
            if check_active is not None:
                # Also checked here so a long run of unchanged threads can be stopped.
                check_active()
            if not sweep and old and old[:4] == signal:
                stats["unchanged"] += 1
                present[key] = old
                continue

            try:
                count = thread.get("comment_count")
                comments = list(
                    client.iter_comments(thread["id"], None if count is None else int(count))
                )
            except TwistNotFoundError:
                # Deleted (or hidden) between the listing and this call. The next
                # run's listing decides; until then keep what was synced.
                stats["skipped"] += 1
                if old:
                    present[key] = old
                continue

            row = {
                "id": key,
                "title": (thread.get("title") or "").strip() or "(untitled)",
                "content": render_thread(thread, comments, channel, workspace_name),
                "url": thread_url(workspace_id, channel_id, thread["id"]),
                "_deleted": False,
                NODE_SET_COLUMN: node_set(workspace_id, channel_id),
            }
            digest = _digest(row)
            present[key] = [*signal, digest]
            if old and len(old) > 4 and old[4] == digest:
                stats["unchanged"] += 1
                continue
            stats["changed"] += 1
            yield row

    gone = sorted(set(previous) - set(present))
    large = len(gone) >= MASS_DELETE_MIN_ITEMS and len(gone) > max_delete_ratio * len(previous)
    if large and not allow_mass_delete and gone != state.get("pending_delete"):
        # A wrong workspace, channel list or token looks exactly like a mass
        # delete. Hold the deletes back and forget them only when the next run
        # sees the same items gone, so a scheduled sync never needs an override.
        logger.warning(
            "Twist: %d of %d synced items are gone; holding the deletes until the next "
            "sync confirms them.",
            len(gone),
            len(previous),
        )
        present.update({key: previous[key] for key in gone})
        state["pending_delete"] = gone
        stats["held_back"] = len(gone)
        gone = []
    else:
        state.pop("pending_delete", None)
    for key in gone:
        stats["deleted"] += 1
        yield {"id": key, "_deleted": True}

    state["items"] = present
    state["runs"] = runs + 1
    logger.info(
        "Twist: scanned %d thread(s): %d changed, %d unchanged, %d skipped; "
        "%d deletion(s), %d held back.",
        stats["scanned"],
        stats["changed"],
        stats["unchanged"],
        stats["skipped"],
        stats["deleted"],
        stats["held_back"],
    )


def _int_list(
    name: str, values: list[int | str] | tuple[int | str, ...] | None
) -> list[int] | None:
    if values is None:
        return None
    if isinstance(values, str):
        # A bare string would be read one character at a time.
        raise TypeError(f"{name} takes a list of ids, for example [{values!r}].")
    return [int(str(v).strip()) for v in values] or None


def twist_source(
    workspace_id: int | str,
    *,
    access_token: str,
    channel_ids: list[int | str] | None = None,
    include_archived: bool = False,
    full_sync: bool = False,
    full_sync_every: int = DEFAULT_FULL_SYNC_EVERY,
    max_delete_ratio: float = DEFAULT_MAX_DELETE_RATIO,
    allow_mass_delete: bool = False,
    resource_name: str | None = None,
    service: httpx.Client | None = None,
    http_client: httpx.Client | None = None,
    check_active: Callable[[], None] | None = None,
):
    """Return a ``dlt`` resource with one row per Twist thread and channel.

    Hand it to ``cognee.remember(...)`` or ``cognee.add(...)`` with
    ``write_disposition="merge"``.

    Args:
        workspace_id: Twist workspace id (the number after ``/a/`` in a Twist
            URL).
        access_token: OAuth access token (or the integration's test token).
        channel_ids: Channels to sync. Default: every channel the token can see,
            except archived ones that were never synced. A listed channel the
            token cannot see stops the sync.
        include_archived: Also sync archived channels when ``channel_ids`` is not
            given. Explicitly listed channels are always synced.
        full_sync: Re-read every thread's comments this run, to pick up edited
            comments right away.
        full_sync_every: Do that automatically every Nth run (default 10, and
            always on the first run). ``0`` turns it off.
        max_delete_ratio: When a run finds more than this share of the synced
            items gone (and at least 10 of them), it holds the deletes back until
            the next run sees the same items gone.
        allow_mass_delete: Forget such a batch at once, for an intended cleanup.
        resource_name: dlt resource name, also the staging table and the key of
            the sync state. Default ``twist_<workspace_id>``.
        service: Pre-built ``httpx`` client (the name cognee's own connectors
            use); ``http_client`` is the same thing. Mainly for tests.
        check_active: Optional callable a host uses to stop a running sync; it
            raises to abort.

    ``cognee_sync_stats`` on the returned resource holds counts only: scanned,
    changed, unchanged, deleted, skipped, held_back, and failed, failed_auth,
    failed_rate_limit (set to 1 when a run stops on that kind of error).
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError(
            "The Twist connector needs a cognee build that reads per-row node sets "
            "(DOCUMENT_SYNC_VERSION >= 2, cognee 1.6.3 or later)."
        )
    if workspace_id is None or not str(workspace_id).strip():
        raise ValueError("workspace_id is required.")
    resolved_workspace_id = int(str(workspace_id).strip())
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError("An access token is required.")
    token = access_token.strip()
    if service is not None and http_client is not None and service is not http_client:
        raise ValueError("Pass service or http_client, not both.")
    injected = service or http_client
    selected = _int_list("channel_ids", channel_ids)
    if full_sync_every < 0:
        raise ValueError("full_sync_every must be 0 or more.")
    if not 0 < max_delete_ratio <= 1:
        raise ValueError("max_delete_ratio must be above 0 and at most 1.")

    name = resource_name or f"twist_{resolved_workspace_id}"
    stats: dict[str, int] = {}
    _reset_stats(stats)

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}, **NODE_SET_COLUMN_HINT},
    )
    def twist_threads():
        _reset_stats(stats)
        owned = None if injected is not None else httpx.Client(timeout=30.0)
        try:
            client = TwistClient(token, http_client=injected or owned)
            state = dlt.current.resource_state()
            known_workspace = state.get("workspace_id")
            if state.get("items") and known_workspace not in (None, resolved_workspace_id):
                # Same resource name, other workspace: syncing on would forget
                # everything the other workspace synced.
                raise ValueError(
                    f"Twist resource {name!r} already holds another workspace's threads. "
                    "Give each Twist source its own resource_name."
                )
            state["workspace_id"] = resolved_workspace_id
            yield from dlt_utils.guarded_rows(
                _iter_rows(
                    client,
                    workspace_id=resolved_workspace_id,
                    state=state,
                    stats=stats,
                    channel_ids=selected,
                    include_archived=include_archived,
                    full_sync=full_sync,
                    full_sync_every=full_sync_every,
                    max_delete_ratio=max_delete_ratio,
                    allow_mass_delete=allow_mass_delete,
                    check_active=check_active,
                ),
                check_active,
            )
        except Exception as exc:
            # Categories only, never the message: a host shows these counts.
            stats["failed"] = 1
            if isinstance(exc, TwistAuthError):
                stats["failed_auth"] = 1
            elif isinstance(exc, TwistRateLimitError):
                stats["failed_rate_limit"] = 1
            raise
        finally:
            if owned is not None:
                owned.close()

    resource = twist_threads()
    # Each thread row (id/title/content/url plus cognee_node_set) becomes a text
    # document that goes through normal cognify.
    setattr(resource, DOCUMENT_SOURCE_ATTR, TWIST_SOURCE_NAME)
    # Own dlt pipeline per workspace, so the sync state never mixes with other
    # dlt sources (otherwise all of them share "ingest_dlt_source").
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    # Counts only, never titles or content.
    resource.cognee_sync_stats = stats
    return resource
