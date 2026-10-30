"""Google Calendar connector for cognee — a ``dlt`` source that turns calendars into memory.

Sync one or more Google calendars into cognee, incrementally and with
forget-on-deletion.  The resource produced here is handed directly to
:func:`cognee.remember`::

    import cognee
    from cognee_community_connector_google_calendar import google_calendar_source

    await cognee.remember(
        google_calendar_source(calendar_ids=["primary"]),
        dataset_name="my_calendar",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by event id
        max_rows_per_table=0,        # 0 = no row cap (calendars exceed the default 50)
    )

Design
------
* **Auth** — read-only ``calendar.readonly`` scope.  Settings come from
  cognee's own Google OAuth settings (``GoogleSettings``) under the
  ``GOOGLE_CALENDAR_`` prefix.  Three ways in: a refresh token plus client id
  and secret (headless), the installed-app browser flow with a cached 0600
  token file (local), or a ready-made access token (hosts).
* **Incremental cursor** — one ``nextSyncToken`` per calendar, kept in dlt
  resource state.  It only arrives on the last page, so it is saved once the
  whole round has finished.  ``410 Gone`` means the token expired: that
  calendar gets a full listing, reconciled like a snapshot.
* **Recurring events** — listed with ``singleEvents=False``, so a series is
  one document with its recurrence rule, and a changed single occurrence is
  its own document.
* **Forget-on-delete** — deleted events come back with ``status: cancelled``.
  A cancelled event is forgotten; a cancelled single occurrence of a series
  only forgets that occurrence, never the series.  Deleting a whole series also
  forgets its changed occurrences.  Deletes are emitted with the ``_deleted``
  hard-delete marker and removed by cognee's ``orphan_cleanup``.
* **Errors never delete** — 401, 403, 429 and 5xx raise after the client's
  retries, and dlt keeps the previous state.  Only a confirmed 404 for a whole
  calendar forgets its events.
* **Document path** — each event becomes a text document with the calendar
  name, organizer and attendees with their responses written into the content,
  so cognify links the same person across meetings.

Limitations
-----------
* Attachments and conference details beyond the meeting link are not ingested.
* ``workingLocation`` events are skipped: they repeat every day and carry no
  meeting content.
"""

import html
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from cognee.modules.integrations.google import GoogleAuthError, GoogleSettings
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from pydantic_settings import SettingsConfigDict

logger = get_logger("google_calendar_connector")

CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
TOKEN_URI = "https://oauth2.googleapis.com/token"
DEFAULT_CALENDARS = ("primary",)
# Event types that carry no meeting content and would only add noise.
SKIPPED_EVENT_TYPES = {"workingLocation"}
PAGE_SIZE = 250
# googleapiclient retries 429 and 5xx with exponential backoff this many times.
NUM_RETRIES = 5
ID_PREFIX = "gcal:"


class GoogleCalendarSettings(GoogleSettings):
    """cognee's Google OAuth settings, read from ``GOOGLE_CALENDAR_*``.

    Inherits ``client_id`` and ``client_secret`` from cognee's
    ``GoogleSettings`` and adds what a local or headless sync needs.
    """

    model_config = SettingsConfigDict(env_prefix="GOOGLE_CALENDAR_", extra="ignore")

    refresh_token: str = ""
    credentials_path: str = ""
    token_path: str = ""


@dataclass(frozen=True)
class _CalendarConfig:
    calendar_ids: tuple[str, ...]


# ---------------------------------------------------------------------------
# Auth / service construction
# ---------------------------------------------------------------------------
def _google_libraries():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - installed with this package
        raise ImportError(
            "The Google Calendar connector needs google-api-python-client, google-auth "
            "and google-auth-oauthlib."
        ) from exc
    return Request, Credentials, InstalledAppFlow, build


def build_calendar_service(credentials_path: str, token_path: str) -> Any:
    """Build a Calendar client through the OAuth installed-app flow.

    The first run opens a browser for consent and caches the token at
    ``token_path`` (written 0600); later runs reuse and refresh it.  Mirrors
    cognee's ``build_gmail_service``, with only the calendar.readonly scope.
    """
    request, credentials_cls, installed_app_flow, build = _google_libraries()
    scopes = [CALENDAR_READONLY_SCOPE]
    creds = None
    if os.path.exists(token_path):
        creds = credentials_cls.from_authorized_user_file(token_path, scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(request())
        else:
            if not os.path.exists(credentials_path):
                raise FileNotFoundError(
                    f"Google OAuth client secrets not found at '{credentials_path}'. "
                    "Download an OAuth client (Desktop app) from the Google Cloud Console "
                    "and set GOOGLE_CALENDAR_CREDENTIALS_PATH."
                )
            flow = installed_app_flow.from_client_secrets_file(credentials_path, scopes)
            creds = flow.run_local_server(port=0)
        # The token file holds the long-lived refresh token: keep it private.
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as token_file:
            token_file.write(creds.to_json())
        os.chmod(token_path, 0o600)  # tighten a pre-existing token file too

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def build_calendar_service_from_access_token(access_token: str) -> Any:
    """Build a Calendar client from a short-lived token supplied by a host."""
    _, credentials_cls, _, build = _google_libraries()
    credentials = credentials_cls(token=access_token, scopes=[CALENDAR_READONLY_SCOPE])
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _build_service_from_refresh_token(settings: GoogleCalendarSettings) -> Any:
    """Headless: google-auth refreshes the access token itself when needed."""
    if not (settings.client_id and settings.client_secret):
        raise GoogleAuthError("calendar sign-in", "missing_client_credentials")
    _, credentials_cls, _, build = _google_libraries()
    credentials = credentials_cls(
        token=None,
        refresh_token=settings.refresh_token,
        token_uri=TOKEN_URI,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        scopes=[CALENDAR_READONLY_SCOPE],
    )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def list_calendars(service: Any) -> list[dict]:
    """Every calendar the account can see: id, name and whether it is primary."""
    calendars: list[dict] = []
    page_token = None
    while True:
        response = (
            service.calendarList()
            .list(pageToken=page_token, maxResults=PAGE_SIZE)
            .execute(num_retries=NUM_RETRIES)
        )
        for entry in response.get("items", []):
            calendars.append(
                {
                    "id": entry["id"],
                    "name": entry.get("summaryOverride") or entry.get("summary") or entry["id"],
                    "primary": bool(entry.get("primary")),
                }
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            return calendars


# ---------------------------------------------------------------------------
# Google API errors
# ---------------------------------------------------------------------------
def _status(exc: Exception) -> int | None:
    """HTTP status of a googleapiclient ``HttpError``, or None."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Sync state machine (pure given a service + state dict — unit-testable)
# ---------------------------------------------------------------------------
def _iter_rows(service: Any, config: _CalendarConfig, state: dict, stats: dict | None = None):
    """Yield one row per new or changed event, plus hard-delete tombstones.

    ``state`` is a plain dict standing in for dlt's resource state, so this is
    directly unit-testable.  Every API call happens before the first yield,
    and the state is only written after the last one, so a failed run changes
    nothing.
    """
    if stats is None:
        stats = {}
    stats.clear()
    stats.update(scanned=0, skipped=0, deleted=0, calendars=0)

    old_calendars: dict[str, dict] = dict(state.get("calendars", {}))
    new_calendars: dict[str, dict] = {}
    rows: list[dict] = []
    deletes: list[str] = []

    # Calendars dropped from the config: their events are out of scope now.
    for calendar_id, calendar_state in old_calendars.items():
        if calendar_id not in config.calendar_ids:
            _forget_all(calendar_id, calendar_state.get("known", []), deletes, stats)

    names = _calendar_names(service)
    for calendar_id in config.calendar_ids:
        result = _sync_calendar(
            service,
            calendar_id,
            names.get(calendar_id, calendar_id),
            old_calendars.get(calendar_id, {}),
            rows,
            deletes,
            stats,
        )
        if result is not None:
            new_calendars[calendar_id] = result
            stats["calendars"] += 1

    for row_id in deletes:
        yield {"id": row_id, "_deleted": True}
    yield from rows

    state["calendars"] = new_calendars
    logger.info("Google Calendar: sync stats %s", stats)


def _calendar_names(service: Any) -> dict[str, str]:
    """Calendar id -> display name, with "primary" mapped to the primary calendar."""
    names: dict[str, str] = {}
    for calendar in list_calendars(service):
        names[calendar["id"]] = calendar["name"]
        if calendar["primary"]:
            names["primary"] = calendar["name"]
    return names


def _sync_calendar(service, calendar_id, name, calendar_state, rows, deletes, stats):
    """One sync round for one calendar. Returns its new state, or None if gone."""
    known: set[str] = set(calendar_state.get("known", []))
    token = calendar_state.get("sync_token")

    items = None
    if token:
        try:
            items, token = _list_events(service, calendar_id, token)
        except Exception as exc:
            # 410 is an expired token. A 404 may mean the calendar is gone,
            # which the full listing below confirms before anything is forgotten.
            if _status(exc) not in (404, 410):
                raise
            logger.info("Google Calendar: sync token for '%s' unusable; relisting.", calendar_id)

    snapshot = items is None
    if snapshot:
        try:
            items, token = _list_events(service, calendar_id, None)
        except Exception as exc:
            if _status(exc) != 404:
                raise
            # The calendar was deleted or is no longer shared with this account.
            logger.warning(
                "Google Calendar: calendar '%s' was not found; forgetting it.", calendar_id
            )
            _forget_all(calendar_id, known, deletes, stats)
            return None

    present: set[str] = set()
    changed: dict[str, dict] = {}
    gone: set[str] = set()
    for event in items:
        event_id = event["id"]
        if event.get("status") == "cancelled":
            if not event.get("recurringEventId"):
                # A deleted event or a deleted whole series: forget its changed
                # single occurrences too, whose ids start with the series id.
                gone.add(event_id)
                gone |= {k for k in known if k.startswith(event_id + "_")}
            elif event_id in known:
                # One cancelled occurrence of a series that we stored because it
                # had been changed. The series itself stays.
                gone.add(event_id)
            continue
        if event.get("eventType") in SKIPPED_EVENT_TYPES:
            stats["skipped"] += 1
            continue
        present.add(event_id)
        changed[event_id] = event

    if snapshot:
        # A full listing has every live event, so anything known but missing
        # was deleted while the token was not usable.
        gone |= known - present
    gone -= present

    for event_id in sorted(gone):
        known.discard(event_id)
        stats["deleted"] += 1
        deletes.append(_row_id(calendar_id, event_id))

    for event_id, event in changed.items():
        known.add(event_id)
        stats["scanned"] += 1
        rows.append(_event_row(calendar_id, name, event))

    return {"sync_token": token, "known": sorted(known), "name": name}


def _list_events(service, calendar_id: str, sync_token: str | None) -> tuple[list[dict], str]:
    """Every page of one round. Returns (events, nextSyncToken).

    A sync-token request must keep the same parameters on every page and
    cannot use a time window, so both kinds of round use the same plain
    parameters.
    """
    events: list[dict] = []
    page_token = None
    while True:
        params: dict[str, Any] = {
            "calendarId": calendar_id,
            "maxResults": PAGE_SIZE,
            "singleEvents": False,
        }
        if sync_token:
            params["syncToken"] = sync_token
        if page_token:
            params["pageToken"] = page_token
        response = service.events().list(**params).execute(num_retries=NUM_RETRIES)
        events.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            next_sync_token = response.get("nextSyncToken")
            if not next_sync_token:
                raise RuntimeError(
                    f"Google Calendar round for '{calendar_id}' ended without a nextSyncToken."
                )
            return events, next_sync_token


def _forget_all(calendar_id: str, event_ids, deletes: list[str], stats: dict) -> None:
    for event_id in sorted(event_ids):
        stats["deleted"] += 1
        deletes.append(_row_id(calendar_id, event_id))


def _row_id(calendar_id: str, event_id: str) -> str:
    # The same invitation can sit in several calendars under one event id.
    return f"{ID_PREFIX}{calendar_id}:{event_id}"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _event_row(calendar_id: str, calendar_name: str, event: dict) -> dict:
    title = event.get("summary") or "(no title)"
    lines = [f"Calendar event: {title}", f"Calendar: {calendar_name}"]
    start, end = event.get("start") or {}, event.get("end") or {}
    if start.get("date"):
        lines.append(f"When: {_all_day(start['date'], end.get('date'))}")
    else:
        lines.append(f"When: {_when(start)} to {_when(end)}")
    if event.get("location"):
        lines.append(f"Location: {event['location']}")
    if event.get("organizer"):
        lines.append(f"Organizer: {_person(event['organizer'])}")
    attendees = event.get("attendees") or []
    if attendees:
        line = "Attendees: " + ", ".join(
            f"{_person(a)} ({a.get('responseStatus') or 'needsAction'})" for a in attendees
        )
        if event.get("attendeesOmitted"):
            line += ", and more not listed by Google"
        lines.append(line)
    if event.get("recurrence"):
        lines.append("Repeats: " + "; ".join(event["recurrence"]))
    if event.get("recurringEventId"):
        original = _when(event.get("originalStartTime") or {})
        lines.append(f"Changed occurrence of series {event['recurringEventId']}, was {original}")
    if event.get("status") == "tentative":
        lines.append("Status: tentative")
    if event.get("eventType") and event["eventType"] != "default":
        lines.append(f"Type: {event['eventType']}")
    if event.get("hangoutLink"):
        lines.append(f"Meeting link: {event['hangoutLink']}")
    description = _plain_text(event.get("description") or "")
    content = "\n".join(lines) + (f"\n\n{description}" if description else "")
    return {
        "id": _row_id(calendar_id, event["id"]),
        "title": title,
        "content": content,
        "url": event.get("htmlLink"),
        "_deleted": False,
    }


def _person(person: dict) -> str:
    name, email = person.get("displayName"), person.get("email")
    if name and email and name != email:
        return f"{name} <{email}>"
    return email or name or "unknown"


def _all_day(first_day: str, exclusive_end: str | None) -> str:
    """The span of an all-day event, with its last day inclusive.

    Google's ``end.date`` is exclusive (the day after the event): a one-day
    event on 2026-11-08 ends on 2026-11-09 and reads "all day, 2026-11-08",
    a three-day one reads "all day, 2026-11-08 to 2026-11-10".
    """
    start = date.fromisoformat(first_day)
    last = date.fromisoformat(exclusive_end) - timedelta(days=1) if exclusive_end else start
    if last <= start:
        return f"all day, {start}"
    return f"all day, {start} to {last}"


def _when(value: dict) -> str:
    moment = value.get("dateTime") or value.get("date") or "unknown"
    zone = value.get("timeZone")
    return f"{moment} ({zone})" if zone else moment


def _plain_text(description: str) -> str:
    """Google allows simple HTML in descriptions; keep the text only."""
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", description, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def google_calendar_source(
    calendar_ids: list[str] | str | None = None,
    *,
    resource_name: str = "google_calendar_events",
    check_active: Callable[[], None] | None = None,
    access_token: str | None = None,
    settings: GoogleCalendarSettings | None = None,
    service: Any = None,
):
    """Return a ``dlt`` resource yielding one row per new or changed calendar event.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        calendar_ids: Calendars to sync, like ``["primary", "team@group.calendar.google.com"]``.
            Defaults to the primary calendar; ``GOOGLE_CALENDAR_IDS`` takes a
            comma-separated list.  ``list_calendars`` shows the ids.
        resource_name: Stable dlt resource name. Give each account its own name
            when several sync into one dataset, so their state stays separate.
        check_active: Optional host authorization checkpoint during extraction.
        access_token: A ready-made token from a host. It is never refreshed.
        settings: Google OAuth settings; read from ``GOOGLE_CALENDAR_*`` when
            omitted.  A refresh token with client id and secret is used first,
            then the installed-app flow with ``credentials_path`` and ``token_path``.
        service: A pre-built Calendar client, mainly for tests.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Google Calendar sync requires a Cognee build with table-scoped DLT document "
            "cleanup. Upgrade Cognee before syncing to avoid deleting another source's data."
        )

    config = _CalendarConfig(calendar_ids=_normalize_calendar_ids(calendar_ids))
    settings = settings or GoogleCalendarSettings()
    access_token = access_token or os.getenv("GOOGLE_CALENDAR_ACCESS_TOKEN")
    if service is None and not (
        access_token or settings.refresh_token or settings.credentials_path
    ):
        # Fail when the source is built, not halfway through a pipeline run.
        raise ValueError(
            "Google Calendar credentials are missing. Set GOOGLE_CALENDAR_REFRESH_TOKEN with "
            "GOOGLE_CALENDAR_CLIENT_ID and GOOGLE_CALENDAR_CLIENT_SECRET, or "
            "GOOGLE_CALENDAR_CREDENTIALS_PATH and GOOGLE_CALENDAR_TOKEN_PATH, or "
            "GOOGLE_CALENDAR_ACCESS_TOKEN for a quick try."
        )

    def make_service() -> Any:
        if service is not None:
            return service
        if access_token:
            return build_calendar_service_from_access_token(access_token)
        if settings.refresh_token:
            return _build_service_from_refresh_token(settings)
        return build_calendar_service(
            os.path.expanduser(settings.credentials_path),
            os.path.expanduser(settings.token_path or "~/.cognee_google_calendar_token.json"),
        )

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
    def google_calendar_events():
        yield from dlt_utils.guarded_rows(
            _iter_rows(make_service(), config, dlt.current.resource_state(), stats),
            check_active,
        )

    resource = google_calendar_events()
    # Opt into the document ingestion path: each event becomes a text document
    # that flows through normal cognify (LLM graph extraction).
    setattr(resource, DOCUMENT_SOURCE_ATTR, "google_calendar")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics contain counts only, never titles or content.
    resource.cognee_sync_stats = stats
    return resource


def _normalize_calendar_ids(calendar_ids: list[str] | str | None) -> tuple[str, ...]:
    if calendar_ids is None:
        env_ids = os.getenv("GOOGLE_CALENDAR_IDS", "")
        calendar_ids = [c for c in env_ids.split(",") if c.strip()] or list(DEFAULT_CALENDARS)
    elif isinstance(calendar_ids, str):
        calendar_ids = [calendar_ids]
    cleaned = tuple(dict.fromkeys(c.strip() for c in calendar_ids if c.strip()))
    if not cleaned:
        # An empty list would forget every synced event, so refuse it.
        raise ValueError('calendar_ids is empty. Pass calendars like ["primary"].')
    return cleaned
