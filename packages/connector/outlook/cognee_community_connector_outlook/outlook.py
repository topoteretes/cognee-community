"""Outlook connector for cognee — a ``dlt`` source that turns mail and calendar into memory.

Sync selected Outlook mail folders and the primary calendar into cognee
through Microsoft Graph, incrementally and with forget-on-deletion.  The
resource produced here is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_outlook import outlook_source

    await cognee.remember(
        outlook_source(mail_folders=["inbox", "sentitems"], client_id=..., token_cache_path=...),
        dataset_name="my_outlook",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by message and event id
        max_rows_per_table=0,        # 0 = no row cap (mailboxes exceed the default 50)
    )

Microsoft Graph delta queries keep one ``deltaLink`` per mail folder and one for
the calendar window.  A message is forgotten once it is in none of the synced
folders, and a removed event is looked up by id first, since Graph also reports
events that only left the window.  Mail and calendar fail separately, and errors
never delete.  The README explains the sync model, the retry rules and the
limitations.
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.sources.helpers.requests import Client
from dlt.sources.helpers.rest_client import RESTClient
from dlt.sources.helpers.rest_client.paginators import BaseNextUrlPaginator
from requests import HTTPError, RequestException

from .graph_auth import (
    CALENDARS_READ,
    DEFAULT_AUTHORITY,
    MAIL_READ,
    MAIL_READ_BASIC,
    USER_READ,
    GraphAuth,
    GraphTokenProvider,
)

logger = get_logger("outlook_connector")

# datetime.UTC only exists from Python 3.11.
UTC = timezone.utc
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
# dlt's defaults sleep Retry-After up to 300 s for 5 attempts, while cognee
# holds its dlt staging lock.  A minute and 3 attempts at most here; after that
# the part stops with a rate-limit error and the next run picks it up.
MAX_RETRY_WAIT_SECONDS = 60
MAX_REQUEST_ATTEMPTS = 3
# dlt uses the resource name as a table name and a state key, so it has to be
# already normalized and short enough to be part of a file name.
_RESOURCE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
DEFAULT_MAIL_FOLDERS = ("inbox", "sentitems")
WELL_KNOWN_FOLDER_NAMES = {
    "inbox": "Inbox",
    "sentitems": "Sent Items",
    "archive": "Archive",
    "drafts": "Drafts",
}
MAIL_FIELDS = [
    "subject",
    "from",
    "toRecipients",
    "ccRecipients",
    "sentDateTime",
    "receivedDateTime",
    "conversationId",
    "webLink",
    "hasAttachments",
]
# Error codes Graph uses when a delta token can no longer be used.
# Compared case-insensitively: Graph's docs and responses differ in casing.
RESYNC_ERROR_CODES = {"syncstatenotfound", "syncstateinvalid", "resyncrequired"}
# Row ids are prefixed so mail and events can never collide in one table.
MAIL_ID_PREFIX = "mail:"
EVENT_ID_PREFIX = "event:"
PAGE_SIZE = 50
PREFER_HEADERS = {
    "Prefer": ", ".join(
        [
            'IdType="ImmutableId"',
            'outlook.body-content-type="text"',
            f"odata.maxpagesize={PAGE_SIZE}",
        ]
    )
}


@dataclass(frozen=True)
class _OutlookConfig:
    include_mail: bool
    mail_folders: tuple[str, ...]
    include_mail_bodies: bool
    mail_received_after: str | None
    include_calendar: bool
    calendar_days_back: int
    calendar_days_ahead: int
    calendar_window_refresh_days: int


class _ODataNextLinkPaginator(BaseNextUrlPaginator):
    """Follows Graph's ``@odata.nextLink`` until the round's last page."""

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[str] = set()

    def update_state(self, response, data=None) -> None:
        link = response.json().get("@odata.nextLink")
        if link in self._seen:
            # The link carries the delta token, so it stays out of the message.
            raise OutlookError("Microsoft Graph returned the same page twice; stopping.")
        if link:
            self._seen.add(link)
        self._next_reference = link


class OutlookError(RuntimeError):
    """A Microsoft Graph request or delta round failed.

    The message holds the HTTP status at most, never a message, event or
    folder id, nor the request URL that requests puts in its own error text.
    """

    def __init__(self, message: str, status: int | None = None, connection: bool = False):
        super().__init__(message)
        self.status = status
        # Graph could not be reached at all, so every later call would fail too.
        self.connection = connection


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------
def build_graph_client(auth: Any, base_url: str = GRAPH_BASE_URL) -> RESTClient:
    session = Client(
        request_max_attempts=MAX_REQUEST_ATTEMPTS,
        request_max_retry_delay=MAX_RETRY_WAIT_SECONDS,
        raise_for_status=False,
    ).session
    return RESTClient(base_url=base_url, auth=auth, session=session)


def _check(check_active: Callable[[], None] | None) -> None:
    """The host's authorization checkpoint, run before every Graph request."""
    if check_active is not None:
        check_active()


def _delta_round(
    client: RESTClient, url: str, params: dict | None, check_active=None
) -> tuple[list[dict], str]:
    """Read every page of one delta round. Returns (items, deltaLink)."""
    items: list[dict] = []
    delta_link = None
    _check(check_active)
    for page in client.paginate(
        url,
        params=params,
        headers=PREFER_HEADERS,
        paginator=_ODataNextLinkPaginator(),
        data_selector="value",
    ):
        items.extend(page)
        delta_link = page.response.json().get("@odata.deltaLink") or delta_link
        # The paginator requests the next page when the loop continues.
        _check(check_active)
    if not delta_link:
        # Without it the next run could not resume, so treat it as a failure.
        raise OutlookError("Microsoft Graph delta round ended without a deltaLink.")
    return items, delta_link


def _status(exc: HTTPError) -> int | None:
    return exc.response.status_code if exc.response is not None else None


def _error_code(exc: HTTPError) -> str | None:
    try:
        return exc.response.json()["error"]["code"]
    except Exception:
        return None


def _needs_resync(exc: HTTPError) -> bool:
    return _status(exc) == 410 or (_error_code(exc) or "").lower() in RESYNC_ERROR_CODES


def _exists(client: RESTClient, path: str, check_active=None) -> dict | None:
    """GET one item: its JSON, or None on a confirmed 404. Other errors raise."""
    _check(check_active)
    response = client.get(path, headers=PREFER_HEADERS)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


# Returned by _lookup when one item could not be read.
_FAILED = object()


def _lookup(client: RESTClient, path: str, check_active=None) -> Any:
    """Like _exists, but one unreadable item returns _FAILED instead of raising.

    A 403 or a 5xx on a single event must not stop the whole calendar on every
    run.  A 401, a 429 and a lost connection still raise: every later request
    would fail the same way.
    """
    try:
        return _exists(client, path, check_active)
    except HTTPError as exc:
        if _status(exc) in (401, 429):
            raise
        return _FAILED


# ---------------------------------------------------------------------------
# Sync state machine (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
_PART_STATS = {
    "mail": ("mail_changed", "mail_deleted"),
    "calendar": ("events_changed", "events_deleted", "events_left_window", "events_failed"),
}
_FAILURE_REASONS = ("auth", "rate_limit", "connection", "mail", "calendar")


def _iter_rows(
    client: RESTClient,
    config: _OutlookConfig,
    state: dict,
    stats: dict[str, int] | None = None,
    now: datetime | None = None,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict]:
    """Yield one row per new or changed message and event, plus tombstones.

    ``state`` is a plain dict standing in for dlt's resource state, so this is
    directly unit-testable.  Every Graph call happens before the first yield
    and the state is only written after the last one.  Mail and calendar each
    keep their old state when they fail; the run raises only when neither
    finished (see the module docstring).
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(
        mail_changed=0,
        mail_deleted=0,
        events_changed=0,
        events_deleted=0,
        events_left_window=0,
        events_failed=0,
        failed=0,
    )
    stats.update({f"failed_{reason}": 0 for reason in _FAILURE_REASONS})
    now = now or datetime.now(UTC)

    parts = [p for p in ("mail", "calendar") if getattr(config, f"include_{p}")]
    finished: dict[str, tuple[dict, list[dict], list[str]]] = {}
    last_error: OutlookError | None = None
    for part in parts:
        # Each part works on its own lists and counters, so a part that fails
        # halfway leaves nothing behind.
        rows: list[dict] = []
        deletes: list[str] = []
        part_stats = dict.fromkeys(_PART_STATS[part], 0)
        try:
            if part == "mail":
                new_state = _sync_mail(
                    client, config, state.get("mail", {}), rows, deletes, part_stats, check_active
                )
            else:
                new_state = _sync_calendar(
                    client,
                    config,
                    state.get("calendar", {}),
                    rows,
                    deletes,
                    part_stats,
                    now,
                    check_active,
                )
        except (RequestException, OutlookError) as exc:
            last_error = exc if isinstance(exc, OutlookError) else _graph_error(exc)
            reason = _failure_reason(last_error) or part
            stats["failed"] += 1
            stats[f"failed_{reason}"] += 1
            logger.warning(
                "Outlook: the %s sync failed (HTTP %s); it keeps its state and is retried "
                "on the next run.",
                part,
                last_error.status,
            )
            if reason in ("auth", "rate_limit", "connection"):
                break  # the other part would fail the same way
            continue
        stats.update(part_stats)
        finished[part] = (new_state, rows, deletes)

    if last_error is not None and not finished:
        # Nothing finished, so there is no state to save and nothing is lost by raising.
        raise OutlookError(f"Outlook sync failed: {last_error}", last_error.status) from None

    # Totals under the keys the other connectors publish, for host dashboards.
    # ``failed`` counts what the next run tries again: whole parts and events.
    stats["scanned"] = stats["mail_changed"] + stats["events_changed"]
    stats["deleted"] = stats["mail_deleted"] + stats["events_deleted"]
    stats["failed"] += stats["events_failed"]

    for _state, _rows, deletes in finished.values():
        for row_id in deletes:
            yield {"id": row_id, "_deleted": True}
    for _state, rows, _deletes in finished.values():
        yield from rows

    for part, (new_state, _rows, _deletes) in finished.items():
        state[part] = new_state
    logger.info("Outlook: sync stats %s", stats)


def _graph_error(exc: RequestException) -> OutlookError:
    """A clean error: requests puts the URL, with ids and the delta token, in its text."""
    response = getattr(exc, "response", None)
    status = response.status_code if response is not None else None
    if status is None:
        return OutlookError("Microsoft Graph could not be reached.", connection=True)
    return OutlookError(f"Microsoft Graph request failed: HTTP {status}", status)


def _failure_reason(error: OutlookError) -> str | None:
    """Why a part failed. Auth, throttling and a lost connection end the run."""
    if error.connection:
        return "connection"
    if error.status == 401:
        return "auth"
    if error.status == 429:
        return "rate_limit"
    return None


# --- mail ------------------------------------------------------------------
def _sync_mail(client, config, mail_state, rows, deletes, stats, check_active=None) -> dict:
    """One delta round per selected folder. Returns the new mail state."""
    old_links: dict[str, str] = dict(mail_state.get("delta_links", {}))
    # message id -> folders it is currently in, across the synced folders.
    members: dict[str, list[str]] = {k: list(v) for k, v in mail_state.get("members", {}).items()}
    new_links: dict[str, str] = {}
    touched: set[str] = set()
    changed: dict[str, tuple[dict, str]] = {}

    def leave(message_id: str, folder: str) -> None:
        if folder in members.get(message_id, []):
            members[message_id].remove(folder)
        touched.add(message_id)

    def leave_folder(folder: str) -> None:
        for message_id, folders in members.items():
            if folder in folders:
                leave(message_id, folder)

    # Folders dropped from the config: their mail is out of scope now.
    for folder in old_links:
        if folder not in config.mail_folders:
            leave_folder(folder)

    params = {"$select": ",".join(_mail_fields(config))}
    if config.mail_received_after:
        params["$filter"] = f"receivedDateTime ge {config.mail_received_after}"

    for folder in config.mail_folders:
        link = old_links.get(folder)
        items = None
        if link:
            try:
                items, new_links[folder] = _delta_round(client, link, None, check_active)
            except HTTPError as exc:
                if not (_needs_resync(exc) or _status(exc) == 404):
                    raise
                logger.info("Outlook: a mail folder's delta token expired; listing it again.")
        if items is None:
            # First run or an expired token: a fresh round lists the whole
            # folder, so reconcile it like a snapshot.
            try:
                items, new_links[folder] = _delta_round(
                    client, f"/me/mailFolders/{folder}/messages/delta", params, check_active
                )
            except HTTPError as exc:
                if (
                    _status(exc) != 404
                    or _exists(client, f"/me/mailFolders/{folder}", check_active) is not None
                ):
                    raise
                logger.warning("Outlook: a synced mail folder was not found; forgetting its mail.")
                new_links.pop(folder, None)
                leave_folder(folder)
                continue
            leave_folder(folder)

        for item in items:
            message_id = item["id"]
            if "@removed" in item:
                leave(message_id, folder)
                continue
            if folder not in members.setdefault(message_id, []):
                members[message_id].append(folder)
            touched.add(message_id)
            changed[message_id] = (item, folder)

    for message_id in sorted(touched):
        if members.get(message_id):
            continue
        # In none of the synced folders any more: deleted, moved to Deleted
        # Items or Junk, or moved to a folder that is not synced.
        members.pop(message_id, None)
        stats["mail_deleted"] += 1
        deletes.append(MAIL_ID_PREFIX + message_id)

    for message_id, (item, folder) in changed.items():
        if members.get(message_id):
            stats["mail_changed"] += 1
            rows.append(_mail_row(item, folder, config.include_mail_bodies))

    return {"delta_links": new_links, "members": members}


def _mail_fields(config: _OutlookConfig) -> list[str]:
    return [*MAIL_FIELDS, "body"] if config.include_mail_bodies else list(MAIL_FIELDS)


def _mail_row(item: dict, folder: str, include_body: bool = True) -> dict:
    subject = item.get("subject") or "(no subject)"
    lines = [
        f"Email: {subject}",
        f"From: {_person(item.get('from'))}",
        f"To: {_people(item.get('toRecipients'))}",
    ]
    if item.get("ccRecipients"):
        lines.append(f"Cc: {_people(item['ccRecipients'])}")
    lines += [
        f"Sent: {item.get('sentDateTime') or item.get('receivedDateTime') or 'unknown'}",
        f"Folder: {WELL_KNOWN_FOLDER_NAMES.get(folder, folder)}",
    ]
    if item.get("conversationId"):
        lines.append(f"Thread: {item['conversationId']}")
    if item.get("hasAttachments"):
        lines.append("Attachments: yes, not ingested")
    # Headers-only mode never renders a body, even if Graph sent one.
    body = ((item.get("body") or {}).get("content") or "").strip() if include_body else ""
    content = "\n".join(lines) + (f"\n\n{body}" if body else "")
    return {
        "id": MAIL_ID_PREFIX + item["id"],
        "title": subject,
        "content": content,
        "url": item.get("webLink"),
        "_deleted": False,
    }


# --- calendar --------------------------------------------------------------
def _sync_calendar(
    client, config, calendar_state, rows, deletes, stats, now, check_active=None
) -> dict:
    """One delta round over the date window. Returns the new calendar state."""
    known: set[str] = set(calendar_state.get("known", []))
    # Events whose lookup failed last run: a removal still to confirm, and a
    # series master still to fetch.  Delta rounds won't list them again.
    recheck: set[str] = set(calendar_state.get("recheck", []))
    retry_masters: set[str] = set(calendar_state.get("retry_masters", []))
    link = calendar_state.get("delta_link")
    window = {
        "days_back": config.calendar_days_back,
        "days_ahead": config.calendar_days_ahead,
    }
    opened_at = calendar_state.get("opened_at")
    stale = (
        not link
        or calendar_state.get("window") != window
        or opened_at is None
        or now - datetime.fromisoformat(opened_at)
        >= timedelta(days=config.calendar_window_refresh_days)
    )

    items = None
    if not stale:
        try:
            items, link = _delta_round(client, link, None, check_active)
        except HTTPError as exc:
            if not _needs_resync(exc):
                raise
            logger.info("Outlook: calendar delta token expired; resyncing.")

    snapshot = items is None
    if snapshot:
        # A calendarView window is fixed for the life of its deltaLink, so a
        # rolling window needs a fresh round now and then.
        start = now - timedelta(days=config.calendar_days_back)
        end = now + timedelta(days=config.calendar_days_ahead)
        items, link = _delta_round(
            client,
            "/me/calendarView/delta",
            {"startDateTime": _iso(start), "endDateTime": _iso(end)},
            check_active,
        )
        opened_at = now.isoformat()

    removed: set[str] = set()
    seen: set[str] = set()
    changed: dict[str, dict] = {}
    series_ids: set[str] = set()
    for item in items:
        event_id = item["id"]
        if "@removed" in item:
            removed.add(event_id)
        elif item.get("type") == "occurrence":
            # Graph lists each occurrence of a recurring series with only its
            # id and times; the series master carries the details and becomes
            # the one document for the whole series.
            seen.add(event_id)
            removed.discard(event_id)
            if item.get("seriesMasterId"):
                series_ids.add(item["seriesMasterId"])
        elif item.get("isCancelled"):
            seen.add(event_id)
            removed.discard(event_id)
            _forget_event(event_id, known, deletes, stats)
        else:
            seen.add(event_id)
            removed.discard(event_id)
            changed[event_id] = item
    # A series that started before the window still has occurrences inside
    # it, but Graph may not list its master. The occurrences prove the series
    # is alive, and a master missing from both this round and memory is
    # fetched by id so the meeting is not lost.
    next_masters: list[str] = []
    for master_id in sorted((series_ids | retry_masters) - seen - set(changed) - known):
        master = _lookup(client, f"/me/events/{master_id}", check_active)
        if master is _FAILED:
            next_masters.append(master_id)
            stats["events_failed"] += 1
        elif master is not None and not master.get("isCancelled"):
            changed[master_id] = master
    seen |= series_ids

    if snapshot:
        # Known events missing from the fresh listing were deleted or left
        # the window; the lookup below tells which.
        removed |= known - seen
    removed |= recheck

    next_recheck: list[str] = []
    for event_id in sorted(removed - seen):
        # Graph also reports an event that left the window as removed, so
        # check it before forgetting anything.
        event = _lookup(client, f"/me/events/{event_id}", check_active)
        if event is _FAILED:
            # Not confirmed either way: keep it, and ask again next run.
            next_recheck.append(event_id)
            stats["events_failed"] += 1
        elif event is None or event.get("isCancelled"):
            _forget_event(event_id, known, deletes, stats)
        else:
            known.discard(event_id)
            stats["events_left_window"] += 1

    for event_id, item in changed.items():
        known.add(event_id)
        stats["events_changed"] += 1
        rows.append(_event_row(item))

    return {
        "delta_link": link,
        "opened_at": opened_at,
        "window": window,
        "known": sorted(known),
        "recheck": next_recheck,
        "retry_masters": next_masters,
    }


def _forget_event(event_id: str, known: set[str], deletes: list[str], stats: dict) -> None:
    known.discard(event_id)
    stats["events_deleted"] += 1
    deletes.append(EVENT_ID_PREFIX + event_id)


def _event_row(item: dict) -> dict:
    subject = item.get("subject") or "(no title)"
    lines = [f"Calendar event: {subject}"]
    if item.get("isAllDay"):
        lines.append(f"When: {_all_day(item.get('start'), item.get('end'))}")
    else:
        lines.append(f"When: {_when(item.get('start'))} to {_when(item.get('end'))}")
    location = (item.get("location") or {}).get("displayName")
    if location:
        lines.append(f"Location: {location}")
    lines.append(f"Organizer: {_person(item.get('organizer'))}")
    # Graph's order is not stable between fetches, and the content hash decides
    # whether the event is re-cognified.
    attendees = sorted(item.get("attendees") or [], key=_person_key)
    if attendees:
        lines.append(
            "Attendees: "
            + ", ".join(
                f"{_person(a)} ({(a.get('status') or {}).get('response') or 'none'})"
                for a in attendees
            )
        )
    if item.get("seriesMasterId"):
        lines.append(f"Series: {item['seriesMasterId']}")
    elif (item.get("recurrence") or {}).get("pattern"):
        pattern = item["recurrence"]["pattern"]
        span = item["recurrence"].get("range") or {}
        repeats = f"Repeats: {pattern.get('type')}"
        if span.get("startDate"):
            repeats += f" from {span['startDate']}"
        if span.get("type") == "endDate" and span.get("endDate"):
            repeats += f" until {span['endDate']}"
        lines.append(repeats)
    online = (item.get("onlineMeeting") or {}).get("joinUrl")
    if online:
        lines.append(f"Online meeting: {online}")
    body = ((item.get("body") or {}).get("content") or "").strip()
    content = "\n".join(lines) + (f"\n\n{body}" if body else "")
    return {
        "id": EVENT_ID_PREFIX + item["id"],
        "title": subject,
        "content": content,
        "url": item.get("webLink"),
        "_deleted": False,
    }


# --- formatting -------------------------------------------------------------
def _person(recipient: dict | None) -> str:
    address = (recipient or {}).get("emailAddress") or {}
    name, email = address.get("name"), address.get("address")
    if name and email and name != email:
        return f"{name} <{email}>"
    return email or name or "unknown"


def _person_key(recipient: dict) -> str:
    address = (recipient or {}).get("emailAddress") or {}
    return (address.get("address") or address.get("name") or "").lower()


def _people(recipients: list[dict] | None) -> str:
    return ", ".join(_person(r) for r in recipients or []) or "none"


def _when(value: dict | None) -> str:
    value = value or {}
    return f"{value.get('dateTime', 'unknown')} {value.get('timeZone', '')}".strip()


def _all_day(start: dict | None, end: dict | None) -> str:
    """The span of an all-day event, with its last day inclusive.

    Graph ends an all-day event at midnight after its last day: a one-day event
    on 2026-11-08 ends on 2026-11-09, so a three-day one reads "2026-11-08 to
    2026-11-10", and a one-day one needs no "to".
    """
    try:
        first = date.fromisoformat((start or {})["dateTime"][:10])
        last = date.fromisoformat((end or {})["dateTime"][:10]) - timedelta(days=1)
    except (KeyError, TypeError, ValueError):
        return f"all day, {_when(start)}"
    if last <= first:
        return f"all day, {first}"
    return f"all day, {first} to {last}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_mail_folders(mail_folders: list[str] | str | None) -> tuple[str, ...]:
    """Folders from the argument, or Inbox and Sent Items, de-duplicated.

    Well-known names like ``Inbox`` are case-insensitive; folder ids are not.
    """
    if mail_folders is None:
        mail_folders = list(DEFAULT_MAIL_FOLDERS)
    elif isinstance(mail_folders, str):
        mail_folders = [mail_folders]
    cleaned = (f.strip() for f in mail_folders)
    return tuple(
        dict.fromkeys(f.lower() if f.lower() in WELL_KNOWN_FOLDER_NAMES else f for f in cleaned)
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def outlook_source(
    mail_folders: list[str] | str | None = None,
    *,
    include_mail: bool = True,
    include_calendar: bool = True,
    include_mail_bodies: bool = True,
    mail_received_after: str | None = None,
    calendar_days_back: int = 180,
    calendar_days_ahead: int = 180,
    calendar_window_refresh_days: int = 7,
    client_id: str | None = None,
    authority: str | None = None,
    token_cache_path: str | None = None,
    access_token: str | None = None,
    resource_name: str = "outlook_items",
    check_active: Callable[[], None] | None = None,
    token_provider: GraphTokenProvider | None = None,
    base_url: str = GRAPH_BASE_URL,
    service: RESTClient | None = None,
):
    """Return a ``dlt`` resource yielding Outlook mail and calendar documents.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.  Settings are taken as arguments only; the example
    reads them from ``OUTLOOK_*`` environment variables.

    Args:
        mail_folders: Folders to sync, as well-known names like ``inbox``,
            ``sentitems`` and ``archive``, or folder ids.  Defaults to Inbox and
            Sent Items.  Deleted Items and Junk should not be synced: moving a
            message there is how Outlook deletes it.
        include_mail: Sync mail.  Turning it off keeps already-synced mail.
        include_calendar: Sync the primary calendar.
        include_mail_bodies: Ingest message bodies.  When False only headers
            are read, which needs only the ``Mail.ReadBasic`` permission.
        mail_received_after: Only mail received on or after this ISO date,
            like ``2026-01-01T00:00:00Z``.  Graph keeps the filter inside the
            delta link.
        calendar_days_back / calendar_days_ahead: The calendar window.
        calendar_window_refresh_days: How often the window moves forward.
        client_id: Entra app (client) id.
        authority: Sign-in authority.  Defaults to ``common``, which accepts
            personal and work or school accounts.
        token_cache_path: Private file for MSAL's token cache, so the
            device-code login happens once.
        access_token: A ready-made Graph token for quick tries.  It is never
            refreshed.
        resource_name: Stable dlt resource name: lowercase letters, digits and
            underscores, up to 64 characters.  Give each mailbox its own name
            when several sync into one dataset, so their state stays separate.
        check_active: Optional host authorization checkpoint, asked before
            every Graph request.
        token_provider: A pre-built ``GraphTokenProvider``, for hosts and tests.
        base_url: Graph endpoint, for tests and national clouds.
        service: A pre-built Graph ``RESTClient``, for hosts and tests.  When
            given, the auth arguments above are not used.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Outlook sync requires a Cognee build with table-scoped DLT document cleanup. "
            "Upgrade Cognee before syncing to avoid deleting another source's data."
        )
    if not isinstance(resource_name, str) or not _RESOURCE_NAME.fullmatch(resource_name):
        # An empty name would silently drop the pipeline scope, and a name dlt
        # normalizes differently would share a table with another mailbox.
        raise ValueError(
            "resource_name must be 1 to 64 lowercase letters, digits or underscores, "
            'starting with a letter, like "work_outlook".'
        )
    if not (include_mail or include_calendar):
        raise ValueError("Nothing to sync: include_mail and include_calendar are both False.")

    mail_folders = _normalize_mail_folders(mail_folders)
    if include_mail and not mail_folders:
        # An empty list would forget every synced message, so refuse it.
        raise ValueError(
            "mail_folders is empty. Pass folders like ['inbox'], or include_mail=False."
        )

    config = _OutlookConfig(
        include_mail=include_mail,
        mail_folders=mail_folders,
        include_mail_bodies=include_mail_bodies,
        mail_received_after=mail_received_after,
        include_calendar=include_calendar,
        calendar_days_back=calendar_days_back,
        calendar_days_ahead=calendar_days_ahead,
        calendar_window_refresh_days=calendar_window_refresh_days,
    )

    if service is not None:
        client = service
    else:
        scopes = [USER_READ]
        if include_mail:
            scopes.append(MAIL_READ if include_mail_bodies else MAIL_READ_BASIC)
        if include_calendar:
            scopes.append(CALENDARS_READ)
        provider = token_provider or GraphTokenProvider(
            client_id=client_id,
            scopes=scopes,
            authority=authority or DEFAULT_AUTHORITY,
            token_cache_path=token_cache_path,
            access_token=access_token,
        )
        client = build_graph_client(GraphAuth(provider), base_url)

    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        write_disposition="merge",
        primary_key="id",
        # `_deleted` is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def outlook_items():
        rows = _iter_rows(
            client, config, dlt.current.resource_state(), stats, check_active=check_active
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = outlook_items()
    # Opt into the document ingestion path: each message and event becomes a
    # text document that flows through normal cognify (LLM graph extraction).
    setattr(resource, DOCUMENT_SOURCE_ATTR, "outlook")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics contain counts only, never subjects or content.
    resource.cognee_sync_stats = stats
    return resource
