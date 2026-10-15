"""Outlook connector for cognee — a ``dlt`` source that turns mail and calendar into memory.

Sync selected Outlook mail folders and the primary calendar into cognee
through Microsoft Graph, incrementally and with forget-on-deletion.  The
resource produced here is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_outlook import outlook_source

    await cognee.remember(
        outlook_source(mail_folders=["inbox", "sentitems"]),
        dataset_name="my_outlook",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by message and event id
        max_rows_per_table=0,        # 0 = no row cap (mailboxes exceed the default 50)
    )

Design
------
* **Auth** — delegated Microsoft Graph sign-in through MSAL's device-code
  flow, shared with the other Microsoft connectors (see ``graph_auth``).
  Read-only scopes: ``Mail.Read`` (or ``Mail.ReadBasic`` without bodies) and
  ``Calendars.Read``.
* **HTTP** — dlt's ``RESTClient``, which retries 429 throttling after
  ``Retry-After`` and transient 5xx errors.  Pages are followed through
  ``@odata.nextLink``.
* **Incremental cursor** — Graph delta queries: one ``deltaLink`` per mail
  folder (``mailFolders/{id}/messages/delta``) and one for the calendar
  window (``calendarView/delta``).  The ``deltaLink`` only arrives on the last
  page, so it is saved in dlt resource state once the whole round finished.
* **Immutable ids** — every request sends ``Prefer: IdType="ImmutableId"``, so
  a message keeps its id when it moves between folders.
* **Forget-on-delete** — mail: a message is forgotten once it is in none of the
  synced folders, so a move between synced folders is an update, and a move to
  Deleted Items (never synced) is a delete.  Calendar: Graph also reports an
  event as removed when it leaves the date window, so a removed event is
  looked up by id first; only a 404, or a cancelled meeting, forgets it.
  Deletes are emitted with the ``_deleted`` hard-delete marker and removed by
  cognee's ``orphan_cleanup``.
* **Errors never delete** — an expired delta token (``410 Gone`` or
  ``syncStateNotFound``) triggers a fresh round reconciled like a snapshot.
  A missing folder is confirmed with a lookup before its mail is forgotten.
  Any other error stops the run, and dlt keeps the previous state.
* **Document path** — mail and events become text documents with sender,
  recipients, organizer and attendees written into the content, so cognify
  links one person across emails and meetings.

Limitations
-----------
* Only the primary calendar: Graph v1.0 event delta covers that one.
* An event that leaves the date window is kept in memory as it was last seen,
  and is no longer tracked for changes.
* Attachments are not ingested, only flagged.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.sources.helpers.rest_client import RESTClient
from dlt.sources.helpers.rest_client.paginators import BaseNextUrlPaginator
from requests import HTTPError

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

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
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

    def update_state(self, response, data=None) -> None:
        self._next_reference = response.json().get("@odata.nextLink")


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------
def build_graph_client(auth: Any, base_url: str = GRAPH_BASE_URL) -> RESTClient:
    return RESTClient(base_url=base_url, auth=auth)


def _delta_round(client: RESTClient, url: str, params: dict | None) -> tuple[list[dict], str]:
    """Read every page of one delta round. Returns (items, deltaLink)."""
    items: list[dict] = []
    delta_link = None
    for page in client.paginate(
        url,
        params=params,
        headers=PREFER_HEADERS,
        paginator=_ODataNextLinkPaginator(),
        data_selector="value",
    ):
        items.extend(page)
        delta_link = page.response.json().get("@odata.deltaLink") or delta_link
    if not delta_link:
        # Without it the next run could not resume, so treat it as a failure.
        raise RuntimeError(f"Microsoft Graph delta round for {url!r} ended without a deltaLink.")
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


def _exists(client: RESTClient, path: str) -> dict | None:
    """GET one item: its JSON, or None on a confirmed 404. Other errors raise."""
    response = client.get(path, headers=PREFER_HEADERS)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Sync state machine (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------
def _iter_rows(
    client: RESTClient,
    config: _OutlookConfig,
    state: dict,
    stats: dict[str, int] | None = None,
    now: datetime | None = None,
):
    """Yield one row per new or changed message and event, plus tombstones.

    ``state`` is a plain dict standing in for dlt's resource state, so this is
    directly unit-testable.  Every Graph call happens before the first yield,
    and the state is only written after the last one, so a failed run changes
    nothing.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(
        mail_changed=0, mail_deleted=0, events_changed=0, events_deleted=0, events_left_window=0
    )
    now = now or datetime.now(UTC)

    rows: list[dict] = []
    deletes: list[str] = []
    new_mail_state = None
    new_calendar_state = None

    if config.include_mail:
        new_mail_state = _sync_mail(client, config, state.get("mail", {}), rows, deletes, stats)
    if config.include_calendar:
        new_calendar_state = _sync_calendar(
            client, config, state.get("calendar", {}), rows, deletes, stats, now
        )

    # Totals under the keys the other connectors publish, for host dashboards.
    stats["scanned"] = stats["mail_changed"] + stats["events_changed"]
    stats["deleted"] = stats["mail_deleted"] + stats["events_deleted"]

    for row_id in deletes:
        yield {"id": row_id, "_deleted": True}
    yield from rows

    if new_mail_state is not None:
        state["mail"] = new_mail_state
    if new_calendar_state is not None:
        state["calendar"] = new_calendar_state
    logger.info("Outlook: sync stats %s", stats)


# --- mail ------------------------------------------------------------------
def _sync_mail(client, config, mail_state, rows, deletes, stats) -> dict:
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
                items, new_links[folder] = _delta_round(client, link, None)
            except HTTPError as exc:
                if not (_needs_resync(exc) or _status(exc) == 404):
                    raise
                logger.info("Outlook: delta token for folder '%s' expired; resyncing.", folder)
        if items is None:
            # First run or an expired token: a fresh round lists the whole
            # folder, so reconcile it like a snapshot.
            try:
                items, new_links[folder] = _delta_round(
                    client, f"/me/mailFolders/{folder}/messages/delta", params
                )
            except HTTPError as exc:
                if _status(exc) != 404 or _exists(client, f"/me/mailFolders/{folder}") is not None:
                    raise
                logger.warning("Outlook: mail folder '%s' was not found; forgetting it.", folder)
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
def _sync_calendar(client, config, calendar_state, rows, deletes, stats, now) -> dict:
    """One delta round over the date window. Returns the new calendar state."""
    known: set[str] = set(calendar_state.get("known", []))
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
            items, link = _delta_round(client, link, None)
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
    for master_id in sorted(series_ids - seen - set(changed) - known):
        master = _exists(client, f"/me/events/{master_id}")
        if master is not None and not master.get("isCancelled"):
            changed[master_id] = master
    seen |= series_ids

    if snapshot:
        # Known events missing from the fresh listing were deleted or left
        # the window; the lookup below tells which.
        removed |= known - seen

    for event_id in sorted(removed - seen):
        # Graph also reports an event that left the window as removed, so
        # check it before forgetting anything.
        event = _exists(client, f"/me/events/{event_id}")
        if event is None or event.get("isCancelled"):
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
    }


def _forget_event(event_id: str, known: set[str], deletes: list[str], stats: dict) -> None:
    known.discard(event_id)
    stats["events_deleted"] += 1
    deletes.append(EVENT_ID_PREFIX + event_id)


def _event_row(item: dict) -> dict:
    subject = item.get("subject") or "(no title)"
    lines = [f"Calendar event: {subject}"]
    if item.get("isAllDay"):
        lines.append(f"When: all day, {_when(item.get('start'))}")
    else:
        lines.append(f"When: {_when(item.get('start'))} to {_when(item.get('end'))}")
    location = (item.get("location") or {}).get("displayName")
    if location:
        lines.append(f"Location: {location}")
    lines.append(f"Organizer: {_person(item.get('organizer'))}")
    attendees = item.get("attendees") or []
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


def _people(recipients: list[dict] | None) -> str:
    return ", ".join(_person(r) for r in recipients or []) or "none"


def _when(value: dict | None) -> str:
    value = value or {}
    return f"{value.get('dateTime', 'unknown')} {value.get('timeZone', '')}".strip()


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_mail_folders(mail_folders: list[str] | str | None) -> tuple[str, ...]:
    """Folders from the argument or OUTLOOK_MAIL_FOLDERS, de-duplicated.

    Well-known names like ``Inbox`` are case-insensitive; folder ids are not.
    """
    if mail_folders is None:
        env_folders = os.getenv("OUTLOOK_MAIL_FOLDERS", "")
        mail_folders = [f for f in env_folders.split(",") if f.strip()] or list(
            DEFAULT_MAIL_FOLDERS
        )
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
):
    """Return a ``dlt`` resource yielding Outlook mail and calendar documents.

    Any argument left as ``None`` falls back to the matching ``OUTLOOK_*``
    environment variable.  Hand the result to ``cognee.remember(...)`` with
    ``write_disposition="merge"`` and ``primary_key="id"``.

    Args:
        mail_folders: Folders to sync, as well-known names like ``inbox``,
            ``sentitems`` and ``archive``, or folder ids.  Defaults to Inbox and
            Sent Items; ``OUTLOOK_MAIL_FOLDERS`` takes a comma-separated list.
            Deleted Items and Junk should not be synced: moving a message there
            is how Outlook deletes it.
        include_mail: Sync mail.  Turning it off keeps already-synced mail.
        include_calendar: Sync the primary calendar.
        include_mail_bodies: Ingest message bodies.  When False only headers
            are read, which needs only the ``Mail.ReadBasic`` permission.
        mail_received_after: Only mail received on or after this ISO date,
            like ``2026-01-01T00:00:00Z``; ``OUTLOOK_MAIL_RECEIVED_AFTER``.
            Graph keeps the filter inside the delta link.
        calendar_days_back / calendar_days_ahead: The calendar window.
        calendar_window_refresh_days: How often the window moves forward.
        client_id: Entra app (client) id; ``OUTLOOK_CLIENT_ID``.
        authority: Sign-in authority; ``OUTLOOK_AUTHORITY``.  Defaults to
            ``common``, which accepts personal and work or school accounts.
        token_cache_path: Private file for MSAL's token cache, so the
            device-code login happens once; ``OUTLOOK_TOKEN_CACHE_PATH``.
        access_token: A ready-made Graph token for quick tries;
            ``OUTLOOK_ACCESS_TOKEN``.  It is never refreshed.
        resource_name: Stable dlt resource name. Give each mailbox its own name
            when several sync into one dataset, so their state stays separate.
        check_active: Optional host authorization checkpoint during extraction.
        token_provider: A pre-built ``GraphTokenProvider``, for hosts and tests.
        base_url: Graph endpoint, for tests and national clouds.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Outlook sync requires a Cognee build with table-scoped DLT document cleanup. "
            "Upgrade Cognee before syncing to avoid deleting another source's data."
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
        mail_received_after=mail_received_after or os.getenv("OUTLOOK_MAIL_RECEIVED_AFTER"),
        include_calendar=include_calendar,
        calendar_days_back=calendar_days_back,
        calendar_days_ahead=calendar_days_ahead,
        calendar_window_refresh_days=calendar_window_refresh_days,
    )

    scopes = [USER_READ]
    if include_mail:
        scopes.append(MAIL_READ if include_mail_bodies else MAIL_READ_BASIC)
    if include_calendar:
        scopes.append(CALENDARS_READ)
    provider = token_provider or GraphTokenProvider(
        client_id=client_id or os.getenv("OUTLOOK_CLIENT_ID"),
        scopes=scopes,
        authority=authority or os.getenv("OUTLOOK_AUTHORITY") or DEFAULT_AUTHORITY,
        token_cache_path=token_cache_path or os.getenv("OUTLOOK_TOKEN_CACHE_PATH"),
        access_token=access_token or os.getenv("OUTLOOK_ACCESS_TOKEN"),
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
        yield from dlt_utils.guarded_rows(
            _iter_rows(client, config, dlt.current.resource_state(), stats), check_active
        )

    resource = outlook_items()
    # Opt into the document ingestion path: each message and event becomes a
    # text document that flows through normal cognify (LLM graph extraction).
    setattr(resource, DOCUMENT_SOURCE_ATTR, "outlook")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics contain counts only, never subjects or content.
    resource.cognee_sync_stats = stats
    return resource
