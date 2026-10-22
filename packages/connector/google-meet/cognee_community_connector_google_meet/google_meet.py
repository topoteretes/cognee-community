"""Google Meet connector for cognee — a ``dlt`` source that turns meetings into memory.

Sync the Google Meet conferences you organized into cognee: who joined and
when, plus transcripts where Google made one.  Incremental, and transcripts
are forgotten when their Google Doc is deleted.  The resource produced here is
handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_google_meet import google_meet_source

    await cognee.remember(
        google_meet_source(),
        dataset_name="my_meetings",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by row id
        max_rows_per_table=0,        # 0 = no row cap
    )

Meet has no change feed, so the newest finished conference's start time is the
cursor, and conferences still running or with a transcript still being written
wait in a pending list.  Each conference and each transcript is its own
document, so deleting a transcript's Google Doc forgets the words but keeps the
attendance.  Expiry at Meet is not a delete, errors never delete, and one bad
conference never blocks the others.  The README explains the sync model, the
failure counts in ``cognee_sync_stats`` and the limitations.
"""

import json
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cognee.modules.integrations.google import GoogleAuthError, GoogleSettings
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from pydantic_settings import SettingsConfigDict

logger = get_logger("google_meet_connector")

# datetime.UTC only exists from Python 3.11.
UTC = timezone.utc
# dlt uses the resource name as a table name and a state key, so it has to be
# already normalized and short enough to be part of a file name.
_RESOURCE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Every failure reason _Failures can count, published as plain int counts.
_FAILURE_REASONS = (
    "auth",
    "rate_limit",
    "network",
    "conference",
    "transcript_check",
    "conference_list",
)

MEET_READONLY_SCOPE = "https://www.googleapis.com/auth/meetings.space.readonly"
DRIVE_MEET_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.meet.readonly"
TOKEN_URI = "https://oauth2.googleapis.com/token"
DEFAULT_TOKEN_PATH = "~/.cognee_google_meet_token.json"
# The largest page each list method allows.
RECORD_PAGE_SIZE = 100
PARTICIPANT_PAGE_SIZE = 250
ARTIFACT_PAGE_SIZE = 100
# googleapiclient retries 429 and 5xx with exponential backoff this many times.
NUM_RETRIES = 5
# Later runs list from this long before the newest finished conference.
OVERLAP = timedelta(hours=1)
# Meet deletes a conference record this long after it ends.
RETENTION = timedelta(days=30)
# A transcript still not written this long after its conference ended is
# given up on, so the conference stops being read again every run.
TRANSCRIPT_WAIT = timedelta(days=1)
ID_PREFIX = "gmeet:"
# Google's reasons for a 403 that is a quota rejection. Same list as cognee's gmail.py.
_RATE_LIMIT_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")


class GoogleMeetSettings(GoogleSettings):
    """cognee's Google OAuth settings, read from ``GOOGLE_MEET_*``.

    Inherits ``client_id`` and ``client_secret`` from cognee's
    ``GoogleSettings`` and adds what a local or headless sync needs.
    """

    model_config = SettingsConfigDict(env_prefix="GOOGLE_MEET_", extra="ignore")

    refresh_token: str = ""
    credentials_path: str = ""
    token_path: str = ""
    # A ready-made token from a host. It is never refreshed.
    access_token: str = ""


@dataclass(frozen=True)
class MeetServices:
    """The two Google clients the connector uses, built from one credential.

    ``drive`` is only needed to check that transcript Docs still exist.
    """

    meet: Any
    drive: Any = None


@dataclass(frozen=True, kw_only=True)
class _MeetConfig:
    include_transcripts: bool = True
    check_drive_deletes: bool = True

    @property
    def uses_drive(self) -> bool:
        return self.include_transcripts and self.check_drive_deletes


def _scopes(uses_drive: bool) -> list[str]:
    return [MEET_READONLY_SCOPE, DRIVE_MEET_READONLY_SCOPE] if uses_drive else [MEET_READONLY_SCOPE]


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
            "The Google Meet connector needs google-api-python-client, google-auth "
            "and google-auth-oauthlib."
        ) from exc
    return Request, Credentials, InstalledAppFlow, build


def _services(build, credentials, uses_drive: bool) -> MeetServices:
    meet = build("meet", "v2", credentials=credentials, cache_discovery=False)
    drive = (
        build("drive", "v3", credentials=credentials, cache_discovery=False) if uses_drive else None
    )
    return MeetServices(meet=meet, drive=drive)


def _granted_scopes(token_path: str) -> set[str]:
    """The scopes a cached token file was granted, or an empty set."""
    try:
        with open(token_path, encoding="utf-8") as token_file:
            scopes = json.load(token_file).get("scopes") or []
    except (OSError, ValueError, AttributeError):
        return set()
    return set(scopes.split() if isinstance(scopes, str) else scopes)


def build_meet_services(
    credentials_path: str,
    token_path: str,
    check_drive_deletes: bool = True,
    *,
    login: bool = False,
) -> MeetServices:
    """Build the Meet (and Drive) clients from the token cached at ``token_path``.

    Reuses and refreshes the cached token.  With ``login=True``, as in
    ``examples/login.py``, a missing token, or one that lacks a needed scope,
    opens a browser for consent and the new token is cached (written 0600).
    A sync never passes it, so it never waits for a person: it raises
    ``GoogleAuthError`` instead.  Mirrors cognee's ``build_gmail_service``,
    with only the read-only Meet scopes.
    """
    request, credentials_cls, installed_app_flow, build = _google_libraries()
    scopes = _scopes(check_drive_deletes)
    creds = None
    # google-auth trusts the scopes it is given, so check the file's own list.
    if os.path.exists(token_path) and set(scopes) <= _granted_scopes(token_path):
        creds = credentials_cls.from_authorized_user_file(token_path, scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(request())
        else:
            if not login:
                raise GoogleAuthError("meet sign-in", "login_required")
            if not os.path.exists(credentials_path):
                raise FileNotFoundError(
                    f"Google OAuth client secrets not found at '{credentials_path}'. "
                    "Download an OAuth client (Desktop app) from the Google Cloud Console "
                    "and set GOOGLE_MEET_CREDENTIALS_PATH."
                )
            flow = installed_app_flow.from_client_secrets_file(credentials_path, scopes)
            creds = flow.run_local_server(port=0)
        # The token file holds the long-lived refresh token: keep it private.
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as token_file:
            token_file.write(creds.to_json())
        os.chmod(token_path, 0o600)  # tighten a pre-existing token file too

    return _services(build, creds, check_drive_deletes)


def build_meet_services_from_access_token(
    access_token: str, check_drive_deletes: bool = True
) -> MeetServices:
    """Build the clients from a short-lived token supplied by a host."""
    _, credentials_cls, _, build = _google_libraries()
    credentials = credentials_cls(token=access_token, scopes=_scopes(check_drive_deletes))
    return _services(build, credentials, check_drive_deletes)


def _build_services_from_refresh_token(
    settings: GoogleMeetSettings, check_drive_deletes: bool
) -> MeetServices:
    """Headless: google-auth refreshes the access token itself when needed."""
    if not (settings.client_id and settings.client_secret):
        raise GoogleAuthError("meet sign-in", "missing_client_credentials")
    _, credentials_cls, _, build = _google_libraries()
    credentials = credentials_cls(
        token=None,
        refresh_token=settings.refresh_token,
        token_uri=TOKEN_URI,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        scopes=_scopes(check_drive_deletes),
    )
    return _services(build, credentials, check_drive_deletes)


def _close(services: MeetServices) -> None:
    """Close the clients' connections: httplib2 leaves its sockets open."""
    for client in (services.meet, services.drive):
        if client is not None:
            client.close()


# ---------------------------------------------------------------------------
# Google API calls
# ---------------------------------------------------------------------------
class GoogleMeetError(RuntimeError):
    """A Google Meet or Drive request failed.

    The message holds the HTTP status at most, never a conference or Doc id or
    the request URL that googleapiclient puts in its own error text.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        rate_limited: bool = False,
        network: bool = False,
    ):
        super().__init__(message)
        self.status = status
        # Set where the HttpError is still in hand: its text must not be kept, so the
        # reason Google gave for a 403 would otherwise be lost.
        self.rate_limited = rate_limited
        self.network = network


def _google_errors() -> tuple[Any, Any, tuple[type[BaseException], ...]]:
    """Google's HTTP error, its refresh error, and the connection errors.

    Imported per request, so a source built on pre-made services (tests,
    hosts) only needs what is installed.
    """
    from google.auth.exceptions import RefreshError, TransportError
    from googleapiclient.errors import HttpError
    from httplib2 import HttpLib2Error

    return HttpError, RefreshError, (OSError, HttpLib2Error, TransportError)


def _execute(request: Any, check_active: Callable[[], None] | None) -> Any:
    """Run one API request after the host's authorization checkpoint.

    The checkpoint sits before the call and outside the ``try``, so a revoked
    connection stops the sync between API calls and is never turned into a
    conference failure.  Google's HTTP errors become ``GoogleMeetError``: their
    own text contains the request URL, and with it the conference or Doc id.
    """
    if check_active is not None:
        check_active()
    http_error, refresh_error, transport_errors = _google_errors()
    try:
        return request.execute(num_retries=NUM_RETRIES)
    except http_error as error:
        status = error.resp.status
        raise GoogleMeetError(
            f"Google request failed: HTTP {status}", status, _is_rate_limited(error)
        ) from None
    except refresh_error:
        # The refresh token was revoked or expired: every later call fails too.
        raise GoogleMeetError("Google sign-in was rejected", 401) from None
    except transport_errors:
        # googleapiclient already retried; the connection itself is down.
        raise GoogleMeetError("Google request failed: network error", network=True) from None


def _is_rate_limited(error: Any) -> bool:
    """Whether Google rejected the call for quota: a 429, or a 403 with a rate-limit reason.

    Google often reports quota as 403 ``rateLimitExceeded``, so the status alone
    cannot tell it from lost access.  Same check as cognee's Gmail and Google
    Calendar connectors.
    """
    status = error.resp.status
    if status == 429:
        return True
    if status != 403:
        return False
    details = getattr(error, "error_details", None) or []
    reasons = {d.get("reason") for d in details if isinstance(d, dict)}
    if reasons.intersection(_RATE_LIMIT_REASONS):
        return True
    return any(reason in str(error) for reason in _RATE_LIMIT_REASONS)


def _list_all(
    list_call: Any, key: str, check_active: Callable[[], None] | None, **params: Any
) -> list[dict]:
    """Every item of a paged list call.

    Meet leaves the list key out when there is nothing to return, so an empty
    response is ``{}``.
    """
    items: list[dict] = []
    page_token = None
    seen: set[str] = set()
    while True:
        response = _execute(list_call(pageToken=page_token, **params), check_active)
        items.extend(response.get(key, []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return items
        if page_token in seen:
            raise GoogleMeetError("Google returned the same page token twice; stopping.")
        seen.add(page_token)


def _get_record(meet: Any, conference_id: str, check_active) -> dict | None:
    """One conference record, or None once Meet has expired it."""
    request = meet.conferenceRecords().get(name=f"conferenceRecords/{conference_id}")
    try:
        return _execute(request, check_active)
    except GoogleMeetError as error:
        if error.status != 404:
            raise
        return None


def _get_space(meet: Any, space_name: str, check_active) -> dict:
    """The conference's meeting space, or ``{}`` when it is gone or not shared with us.

    The space only adds the meeting link and code, so losing it must not keep
    the attendance and transcript out of memory.
    """
    try:
        return _execute(meet.spaces().get(name=space_name), check_active)
    except GoogleMeetError as error:
        if error.status not in (403, 404) or error.rate_limited:
            raise
        return {}


def _doc_exists(drive: Any, doc_id: str, check_active) -> bool:
    """False when the transcript Doc is trashed or gone. Other errors raise."""
    try:
        file = _execute(drive.files().get(fileId=doc_id, fields="id,trashed"), check_active)
    except GoogleMeetError as error:
        if error.status != 404:
            raise
        return False
    return not file.get("trashed")


# ---------------------------------------------------------------------------
# Sync state machine (pure given the services + state dict — unit-testable)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Conference:
    """What syncing one finished conference produced."""

    rows: list[dict]
    transcripts: dict[str, str]  # new transcript row id -> Docs file id
    waiting: bool  # a transcript is still being written
    gave_up: bool = False  # a transcript was still not written after TRANSCRIPT_WAIT


class _Failures:
    """Counts failed units of work, and stops the run when the rest would fail too."""

    def __init__(self, stats: dict[str, int]):
        self.stats = stats
        self.last: GoogleMeetError | None = None
        self.stopped = False

    def record(self, error: GoogleMeetError, kind: str) -> None:
        self.last = error
        reason = _failure_reason(error) or kind
        self.stats["failed"] += 1
        self.stats[f"failed_{reason}"] += 1
        logger.warning(
            "Google Meet: a %s failed (HTTP %s); it is kept and retried on the next run.",
            kind.replace("_", " "),
            error.status,
        )
        # Auth, rate limits and a lost connection hit every later call the same way.
        if reason in ("auth", "rate_limit", "network"):
            self.stopped = True


def _failure_reason(error: GoogleMeetError) -> str | None:
    if error.status == 401:
        return "auth"
    if error.rate_limited:
        return "rate_limit"
    if error.network:
        return "network"
    return None


def _iter_rows(
    services: MeetServices,
    config: _MeetConfig,
    state: dict,
    stats: dict[str, int],
    check_active: Callable[[], None] | None = None,
    now: datetime | None = None,
) -> Iterator[dict]:
    """Yield one row per new or finished conference and transcript, plus tombstones.

    ``state`` is a plain dict standing in for dlt's resource state, so this is
    directly unit-testable.  Rows are yielded as each conference finishes, so
    only one conference is held in memory, and the state is written after the
    last row.  A conference that fails stays pending and is retried; the run
    raises only when nothing finished, which is before anything was yielded,
    because dlt drops the state of a failed run and would throw the finished
    work away.
    """
    stats.clear()
    stats.update(
        conferences=0, transcripts=0, transcripts_given_up=0, deleted=0, pending=0, failed=0
    )
    stats.update({f"failed_{reason}": 0 for reason in _FAILURE_REASONS})
    now = now or datetime.now(UTC)
    meet = services.meet

    since = state.get("since")
    newest = _parse_time(since) if since else None
    # Meet has deleted expired conferences, so they can no longer come back:
    # stop tracking them. What cognee already learned from them stays.
    done = {cid: exp for cid, exp in state.get("done", {}).items() if not _expired(exp, now)}
    # A conference that fails or is not reached this run stays pending.
    pending: dict[str, str] = dict(state.get("pending", {}))
    known: dict[str, str] = dict(state.get("transcripts", {}))
    failures = _Failures(stats)
    finished = 0

    try:
        listed = _list_records(meet, since, check_active)
    except GoogleMeetError as error:
        failures.record(error, "conference_list")
        raise

    if not config.include_transcripts:
        # Transcripts are out of scope now: forget the ones already synced.
        for row_id in sorted(known):
            stats["deleted"] += 1
            yield {"id": row_id, "_deleted": True}
        known = {}
    elif config.uses_drive:
        # Meet still lists a transcript after its Doc is deleted; Drive does not.
        for row_id, doc_id in sorted(known.items()):
            if failures.stopped:
                break
            if not doc_id:
                continue
            try:
                exists = _doc_exists(services.drive, doc_id, check_active)
            except GoogleMeetError as error:
                failures.record(error, "transcript_check")  # errors never delete
                continue
            finished += 1
            if not exists:
                stats["deleted"] += 1
                yield {"id": row_id, "_deleted": True}
                del known[row_id]

    records = {_conference_id(r["name"]): r for r in listed}
    records = {cid: record for cid, record in records.items() if cid not in done}
    for conference_id in sorted(pending):
        if failures.stopped:
            break
        if conference_id in records:
            continue
        try:
            record = _get_record(meet, conference_id, check_active)
        except GoogleMeetError as error:
            failures.record(error, "conference")
            continue
        finished += 1
        if record is None:
            # Expired at Meet before it finished: drop it, delete nothing.
            del pending[conference_id]
        else:
            records[conference_id] = record

    spaces: dict[str, dict] = {}
    for conference_id, record in sorted(
        records.items(), key=lambda item: _parse_time(item[1]["startTime"])
    ):
        expire_time = record.get("expireTime", "")
        if failures.stopped:
            # Not reached this run: read it again next time.
            pending[conference_id] = expire_time
            continue
        if not record.get("endTime"):
            # Still running: participants and transcripts are not final yet.
            pending[conference_id] = expire_time
            finished += 1
            continue
        try:
            result = _sync_conference(services, config, record, spaces, known, check_active, now)
        except GoogleMeetError as error:
            failures.record(error, "conference")
            pending[conference_id] = expire_time
            continue
        finished += 1
        yield from result.rows
        known.update(result.transcripts)
        stats["conferences"] += 1
        stats["transcripts"] += len(result.transcripts)
        stats["transcripts_given_up"] += result.gave_up
        if result.waiting:
            pending[conference_id] = expire_time
        else:
            pending.pop(conference_id, None)
            done[conference_id] = expire_time or _stamp(_parse_time(record["endTime"]) + RETENTION)
            started = _parse_time(record["startTime"])
            newest = started if newest is None else max(newest, started)

    if failures.last is not None and not finished:
        # Nothing finished, so there is no state to save and nothing is lost by raising.
        last = failures.last
        raise GoogleMeetError(
            f"Google Meet sync failed: {last}", last.status, last.rate_limited, last.network
        ) from last

    stats["pending"] = len(pending)
    if newest is not None:
        state["since"] = _stamp(newest)
    state["done"] = done
    state["pending"] = pending
    state["transcripts"] = known
    logger.info("Google Meet: sync stats %s", stats)


def _list_records(meet: Any, since: str | None, check_active) -> list[dict]:
    """Conferences that started since the cursor, minus the overlap."""
    params: dict[str, Any] = {"pageSize": RECORD_PAGE_SIZE}
    if since:
        # The overlap catches a conference that started just before the newest
        # finished one but was still running last time; `done` stops repeats.
        params["filter"] = f'start_time>="{_stamp(_parse_time(since) - OVERLAP)}"'
    return _list_all(meet.conferenceRecords().list, "conferenceRecords", check_active, **params)


def _sync_conference(
    services: MeetServices,
    config: _MeetConfig,
    record: dict,
    spaces: dict[str, dict],
    known: dict[str, str],
    check_active: Callable[[], None] | None,
    now: datetime,
) -> _Conference:
    """The rows of one finished conference: itself plus its new transcripts."""
    meet = services.meet
    space_name = record.get("space", "")
    if space_name not in spaces:
        # A recurring meeting reuses its link, so many conferences share a space.
        spaces[space_name] = _get_space(meet, space_name, check_active) if space_name else {}
    space = spaces[space_name]
    participants = _participants(meet, record["name"], check_active)

    transcript_rows: list[dict] = []
    new_transcripts: dict[str, str] = {}
    waiting = False
    has_transcript = False
    if config.include_transcripts:
        for transcript in _list_all(
            meet.conferenceRecords().transcripts().list,
            "transcripts",
            check_active,
            parent=record["name"],
            pageSize=ARTIFACT_PAGE_SIZE,
        ):
            if transcript.get("state") != "FILE_GENERATED":
                # STARTED or ENDED: the Doc is not written yet, come back later.
                waiting = True
                continue
            row_id = _transcript_row_id(transcript["name"])
            if row_id in known:
                # Already synced, and a finished transcript does not change.
                has_transcript = True
                continue
            doc_id = (transcript.get("docsDestination") or {}).get("document", "")
            if (
                doc_id
                and config.uses_drive
                and not _doc_exists(services.drive, doc_id, check_active)
            ):
                # Deleted before we ever stored it: keep it out of memory.
                continue
            entries = _list_all(
                meet.conferenceRecords().transcripts().entries().list,
                "transcriptEntries",
                check_active,
                parent=transcript["name"],
                pageSize=ARTIFACT_PAGE_SIZE,
            )
            has_transcript = True
            transcript_rows.append(
                _transcript_row(record, space, transcript, entries, participants)
            )
            new_transcripts[row_id] = doc_id

    gave_up = waiting and _parse_time(record["endTime"]) + TRANSCRIPT_WAIT <= now
    if gave_up:
        # Google never finished writing it: stop reading this conference every run.
        waiting = False
    recordings = _execute(
        meet.conferenceRecords().recordings().list(parent=record["name"], pageSize=1),
        check_active,
    )
    if not config.include_transcripts:
        transcript_status = None
    elif has_transcript:
        transcript_status = "yes"
    else:
        transcript_status = "being generated" if waiting else "no"
    conference_row = _conference_row(
        record, space, participants, transcript_status, bool(recordings.get("recordings"))
    )
    return _Conference([conference_row, *transcript_rows], new_transcripts, waiting, gave_up)


def _participants(meet: Any, record_name: str, check_active) -> list[tuple[dict, list[dict]]]:
    """Each participant with their sessions, in a stable order.

    Meet returns both lists newest first and does not promise any order, and
    the content hash decides whether a conference is re-cognified.
    """
    people = []
    for participant in _list_all(
        meet.conferenceRecords().participants().list,
        "participants",
        check_active,
        parent=record_name,
        pageSize=PARTICIPANT_PAGE_SIZE,
    ):
        sessions = _list_all(
            meet.conferenceRecords().participants().participantSessions().list,
            "participantSessions",
            check_active,
            parent=participant["name"],
            pageSize=PARTICIPANT_PAGE_SIZE,
        )
        sessions.sort(key=lambda s: (_sort_time(s.get("startTime")), s.get("name", "")))
        people.append((participant, sessions))
    people.sort(
        key=lambda person: (
            _sort_time(person[0].get("earliestStartTime")),
            _participant_name(person[0])[0],
            person[0].get("name", ""),
        )
    )
    return people


def _conference_id(name: str) -> str:
    return name.split("/")[1]


def _transcript_row_id(transcript_name: str) -> str:
    # conferenceRecords/{conference}/transcripts/{transcript}
    parts = transcript_name.split("/")
    return f"{ID_PREFIX}transcript:{parts[1]}/{parts[3]}"


def _expired(expire_time: str, now: datetime) -> bool:
    return bool(expire_time) and _parse_time(expire_time) <= now


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
PARTICIPANT_KINDS = (
    ("signedinUser", "signed in"),
    ("anonymousUser", "guest"),
    ("phoneUser", "phone"),
)


def _conference_row(record, space, participants, transcript_status, recorded) -> dict:
    lines = ["Google Meet conference"]
    lines.extend(_link_lines(space))
    lines.append(f"Started: {_stamp(_parse_time(record['startTime']))}")
    lines.append(f"Ended: {_stamp(_parse_time(record['endTime']))}")
    if participants:
        lines.append("Participants (times in UTC):")
        for participant, sessions in participants:
            name, kind = _participant_name(participant)
            lines.append(f"- {name} ({kind}): {_sessions_text(participant, sessions)}")
    else:
        lines.append("Participants: none recorded")
    if transcript_status is not None:
        lines.append(f"Transcript: {transcript_status}")
    lines.append(f"Recording: {'yes' if recorded else 'no'}")
    return {
        "id": f"{ID_PREFIX}conference:{_conference_id(record['name'])}",
        "title": f"Meet {_meeting_label(record, space)}, {_date(record['startTime'])}",
        "content": "\n".join(lines),
        "url": space.get("meetingUri"),
        "_deleted": False,
    }


def _transcript_row(record, space, transcript, entries, participants) -> dict:
    speakers = {p["name"]: _participant_name(p)[0] for p, _sessions in participants}
    # Meet lists entries oldest first; sorting keeps that even if a page comes back shuffled.
    entries = sorted(entries, key=lambda e: (_sort_time(e.get("startTime")), e.get("name", "")))
    lines = ["Google Meet transcript"]
    lines.extend(_link_lines(space))
    lines.append(f"Conference started: {_stamp(_parse_time(record['startTime']))}")
    lines.append("Transcript (times in UTC):")
    for entry in entries:
        speaker = speakers.get(entry.get("participant", ""), "Unknown speaker")
        when = _clock(entry.get("startTime"), seconds=True)
        lines.append(f"[{when}] {speaker}: {(entry.get('text') or '').strip()}")
    label = _meeting_label(record, space)
    return {
        "id": _transcript_row_id(transcript["name"]),
        "title": f"Meet transcript, {label}, {_date(record['startTime'])}",
        "content": "\n".join(lines),
        "url": (transcript.get("docsDestination") or {}).get("exportUri"),
        "_deleted": False,
    }


def _link_lines(space: dict) -> list[str]:
    # Written exactly like the Google Calendar connector's "Meeting link" line,
    # so a conference and its calendar event share it.
    lines = []
    if space.get("meetingUri"):
        lines.append(f"Meeting link: {space['meetingUri']}")
    if space.get("meetingCode"):
        lines.append(f"Meeting code: {space['meetingCode']}")
    return lines


def _participant_name(participant: dict) -> tuple[str, str]:
    for field, kind in PARTICIPANT_KINDS:
        if field in participant:
            return (participant[field] or {}).get("displayName") or "Unknown participant", kind
    return "Unknown participant", "unknown"


def _sessions_text(participant: dict, sessions: list[dict]) -> str:
    # A participant who left and rejoined has one session per join.
    spans = sessions or [
        {
            "startTime": participant.get("earliestStartTime"),
            "endTime": participant.get("latestEndTime"),
        }
    ]
    return ", ".join(
        f"{_clock(span.get('startTime'))} to "
        f"{_clock(span.get('endTime')) if span.get('endTime') else 'still in the call'}"
        for span in spans
    )


def _meeting_label(record: dict, space: dict) -> str:
    return space.get("meetingCode") or _conference_id(record["name"])


# ---------------------------------------------------------------------------
# Time helpers (Meet timestamps are RFC 3339 in UTC, up to nanoseconds)
# ---------------------------------------------------------------------------
_FRACTION = re.compile(r"\.(\d+)")


def _parse_time(value: str) -> datetime:
    # datetime only keeps microseconds, so trim longer fractions first.
    value = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value, count=1)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _sort_time(value: str | None) -> datetime:
    return _parse_time(value) if value else datetime.max.replace(tzinfo=UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clock(value: str | None, seconds: bool = False) -> str:
    if not value:
        return "unknown"
    return _parse_time(value).strftime("%H:%M:%S" if seconds else "%H:%M")


def _date(value: str) -> str:
    return _parse_time(value).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def google_meet_source(
    *,
    resource_name: str = "google_meet",
    include_transcripts: bool = True,
    check_drive_deletes: bool = True,
    check_active: Callable[[], None] | None = None,
    access_token: str | None = None,
    settings: GoogleMeetSettings | None = None,
    services: MeetServices | None = None,
):
    """Return a ``dlt`` resource yielding conference and transcript rows.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        resource_name: Stable dlt resource name: lowercase letters, digits and
            underscores, up to 64 characters.  Give each account its own name
            when several sync into one dataset, so their state stays separate.
        include_transcripts: Also sync transcripts. Turning it off later forgets
            the transcripts already synced.
        check_drive_deletes: Check every run that synced transcript Docs still
            exist in Drive, and forget the deleted ones.  Needs the
            ``drive.meet.readonly`` scope; without it only the Meet scope is asked.
        check_active: Optional host authorization checkpoint during extraction.
        access_token: A ready-made token from a host. It is never refreshed.
        settings: Google OAuth settings; read from ``GOOGLE_MEET_*`` when
            omitted.  A refresh token with client id and secret is used first,
            then the installed-app flow with ``credentials_path`` and ``token_path``.
        services: Pre-built Meet and Drive clients, mainly for tests.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Google Meet sync requires a Cognee build with table-scoped DLT document "
            "cleanup. Upgrade Cognee before syncing to avoid deleting another source's data."
        )
    if not isinstance(resource_name, str) or not _RESOURCE_NAME.fullmatch(resource_name):
        # An empty name would silently drop the pipeline scope, and a name dlt
        # normalizes differently would share a table with another account.
        raise ValueError(
            "resource_name must be 1 to 64 lowercase letters, digits or underscores, "
            'starting with a letter, like "work_meet".'
        )

    for option, value in (
        ("include_transcripts", include_transcripts),
        ("check_drive_deletes", check_drive_deletes),
    ):
        # A string like "false" would otherwise be read as True.
        if not isinstance(value, bool):
            raise TypeError(f"{option} must be True or False.")
    if check_active is not None and not callable(check_active):
        raise TypeError("check_active must be a function or None.")

    config = _MeetConfig(
        include_transcripts=include_transcripts, check_drive_deletes=check_drive_deletes
    )
    settings = settings or GoogleMeetSettings()
    access_token = access_token or settings.access_token
    if services is None and not (
        access_token or settings.refresh_token or settings.credentials_path
    ):
        # Fail when the source is built, not halfway through a pipeline run.
        raise ValueError(
            "Google Meet credentials are missing. Set GOOGLE_MEET_REFRESH_TOKEN with "
            "GOOGLE_MEET_CLIENT_ID and GOOGLE_MEET_CLIENT_SECRET, or "
            "GOOGLE_MEET_CREDENTIALS_PATH and GOOGLE_MEET_TOKEN_PATH, or "
            "GOOGLE_MEET_ACCESS_TOKEN for a quick try."
        )
    if services is not None and config.uses_drive and services.drive is None:
        raise ValueError(
            "check_drive_deletes needs a Drive client in services, or pass "
            "check_drive_deletes=False."
        )

    def make_services() -> MeetServices:
        if services is not None:
            return services
        if access_token:
            return build_meet_services_from_access_token(access_token, config.uses_drive)
        if settings.refresh_token:
            return _build_services_from_refresh_token(settings, config.uses_drive)
        return build_meet_services(
            os.path.expanduser(settings.credentials_path),
            os.path.expanduser(settings.token_path or DEFAULT_TOKEN_PATH),
            config.uses_drive,
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
    def google_meet_rows():
        # Built per run, so every run gets a fresh token and its own connections.
        run_services = make_services()
        try:
            rows = _iter_rows(
                run_services, config, dlt.current.resource_state(), stats, check_active
            )
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if services is None:
                _close(run_services)

    resource = google_meet_rows()
    # Opt into the document ingestion path: each row becomes a text document
    # that flows through normal cognify (LLM graph extraction).
    setattr(resource, DOCUMENT_SOURCE_ATTR, "google_meet")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    # Host-readable diagnostics contain counts only, never names or content.
    resource.cognee_sync_stats = stats
    return resource
