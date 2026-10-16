"""Zoom connector for cognee: a ``dlt`` source that turns cloud recordings into memory.

Sync the cloud recordings of a Zoom account (meeting metadata, transcripts and
in-meeting chat) into cognee, incrementally and with forget-on-delete. The
resource is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_zoom import zoom_source

    await cognee.remember(
        zoom_source(),  # ZOOM_ACCOUNT_ID / ZOOM_CLIENT_ID / ZOOM_CLIENT_SECRET from env
        dataset_name="zoom",
        primary_key="id",
        write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
        max_rows_per_table=0,
    )

Design
------
* **Auth** is a Server-to-Server OAuth app (``account_credentials`` grant). The
  token lives one hour and has no refresh token, so the client asks for a new
  one before it expires and once more on a 401.
* **Rows** are flat ``{id, title, content, url, _deleted}``, one per meeting
  instance, keyed by the instance ``uuid`` (recurring meetings reuse the meeting
  number). Every column feeds the stored document identity, so nothing volatile
  is rendered.
* **Incremental** over a ``start_time`` window. The window start is stored on
  the first run and reused, and each run lists recording metadata for the whole
  window (one month per request, Zoom's limit). Transcript and chat files are
  downloaded only for meetings whose files changed since the last run.
* **Not-ready transcripts.** A meeting whose files are still processing, or that
  has a recording but no transcript yet, is held back until the next run. After
  ``transcript_wait_hours`` it is ingested with whatever exists.
* **Forget-on-delete.** A meeting that was synced before but is missing from the
  window listing (deleted, trashed, removed by a retention policy) is emitted as
  a hard-delete tombstone. Any request failure raises, so a partial listing can
  never look like a deletion: dlt rolls the state of a failed run back.

Privacy
-------
Transcripts and chat are personal data. Nothing is fetched until you construct a
source and call ``remember``, and anyone with read access to the target dataset
can read what was ingested.
"""

import base64
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = logging.getLogger(__name__)

TOKEN_URL = "https://zoom.us/oauth/token"
DEFAULT_API_URL = "https://api.zoom.us"
DEFAULT_SINCE_DAYS = 30
DEFAULT_TRANSCRIPT_WAIT_HOURS = 24
# The recordings list accepts a date range of at most one month.
_SLICE_DAYS = 30
_TOKEN_MARGIN_SECONDS = 60
_TIMEOUT = 30.0
_RETRIES = 5
_MAX_RETRY_SLEEP = 60.0
_MEDIA_FILE_TYPES = {"MP4", "M4A"}


# ---------------------------------------------------------------------------
# Errors: messages carry status and Zoom error codes only, never secrets or bodies.
# ---------------------------------------------------------------------------
class ZoomSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class ZoomAuthError(ZoomSourceError):
    """Zoom rejected the Server-to-Server OAuth credentials or the access token."""


class ZoomAPIError(ZoomSourceError):
    """Zoom answered with an error, or could not be reached."""

    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _retry_delay(response: Any, attempt: int) -> float:
    try:
        delay = float(response.headers.get("Retry-After"))
    except (TypeError, ValueError):
        delay = float(2**attempt)
    return min(max(delay, 0.0), _MAX_RETRY_SLEEP)


def _error_code(response: Any) -> int | None:
    try:
        code = response.json().get("code")
    except (ValueError, AttributeError):
        return None
    return code if isinstance(code, int) else None


class ZoomClient:
    """Minimal synchronous REST client for a Server-to-Server OAuth app.

    ``get`` returns the decoded JSON body and ``download`` the text of a
    recording file. Tests and hosts can pass any object with the same two
    methods to :func:`zoom_source` instead.
    """

    def __init__(
        self,
        account_id: str,
        client_id: str,
        client_secret: str,
        *,
        http: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not (account_id and client_id and client_secret):
            raise ValueError("Zoom account_id, client_id and client_secret must all be set")
        import httpx

        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._basic_auth = f"Basic {basic}"
        self._account_id = account_id
        # httpx drops the Authorization header when a redirect leaves the host,
        # so the token is not sent to the storage host behind a download_url.
        self._http = http or httpx.Client(timeout=_TIMEOUT, follow_redirects=True)
        self._sleep = sleep
        self._clock = clock
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._api_url = DEFAULT_API_URL + "/v2"

    def __repr__(self) -> str:
        return "ZoomClient(credentials=<redacted>)"

    def _send(self, method: str, url: str, **kwargs: Any) -> Any:
        import httpx

        for attempt in range(_RETRIES):
            last = attempt == _RETRIES - 1
            try:
                response = self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if last:
                    raise ZoomAPIError(f"Zoom request failed: {type(exc).__name__}") from None
                self._sleep(float(2**attempt))
                continue
            if (response.status_code == 429 or response.status_code >= 500) and not last:
                self._sleep(_retry_delay(response, attempt))
                continue
            return response
        raise ZoomAPIError("Zoom request failed")  # pragma: no cover

    def _token(self, force: bool = False) -> str:
        if not force and self._access_token and self._clock() < self._expires_at:
            return self._access_token
        response = self._send(
            "POST",
            TOKEN_URL,
            data={"grant_type": "account_credentials", "account_id": self._account_id},
            headers={"Authorization": self._basic_auth},
        )
        if response.status_code in (400, 401):
            raise ZoomAuthError(
                "Zoom rejected the Server-to-Server OAuth credentials "
                f"(HTTP {response.status_code})"
            )
        if response.status_code != 200:
            raise ZoomAPIError(
                f"Zoom token request failed: HTTP {response.status_code}",
                status=response.status_code,
            )
        body = response.json()
        self._access_token = body["access_token"]
        expires_in = int(body.get("expires_in") or 3600)
        self._expires_at = self._clock() + max(expires_in - _TOKEN_MARGIN_SECONDS, 0)
        # The token response names the API cluster of the account.
        self._api_url = str(body.get("api_url") or DEFAULT_API_URL).rstrip("/") + "/v2"
        return self._access_token

    def _authorized(self, url: str, params: dict[str, Any] | None = None) -> Any:
        response = self._send(
            "GET", url, params=params, headers={"Authorization": f"Bearer {self._token()}"}
        )
        if response.status_code == 401:
            # Revoked or expired early: one fresh token, then give up.
            token = self._token(force=True)
            response = self._send(
                "GET", url, params=params, headers={"Authorization": f"Bearer {token}"}
            )
            if response.status_code == 401:
                raise ZoomAuthError("Zoom rejected the access token (HTTP 401)")
        if response.status_code != 200:
            code = _error_code(response)
            detail = f" (code {code})" if code is not None else ""
            raise ZoomAPIError(
                f"Zoom request failed: HTTP {response.status_code}{detail}",
                status=response.status_code,
                code=code,
            )
        return response

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._token()
        return self._authorized(self._api_url + path, params).json()

    def download(self, url: str) -> str:
        return self._authorized(url).content.decode("utf-8-sig", errors="replace")


# ---------------------------------------------------------------------------
# Parsing: Zoom transcripts (WebVTT) and in-meeting chat (TXT)
# ---------------------------------------------------------------------------
_CHAT_LINE = re.compile(r"^(\d{1,2}:\d{2}:\d{2})\s+(.*)$")
_TO_EVERYONE = re.compile(r"\s+to\s+everyone$", re.IGNORECASE)


def parse_vtt(text: str) -> list[str]:
    """Return transcript turns as ``Speaker: text``, timestamps dropped.

    Consecutive cues of one speaker are merged into one turn.
    """
    lines = [line.strip() for line in text.splitlines()]
    turns: list[list[str]] = []
    for index, line in enumerate(lines):
        if not line or line.startswith(("WEBVTT", "NOTE")) or "-->" in line:
            continue
        following = lines[index + 1] if index + 1 < len(lines) else ""
        if line.isdigit() and "-->" in following:
            continue  # cue number
        speaker, sep, said = line.partition(": ")
        if not sep:
            speaker, said = "", line
        said = said.strip()
        if not said:
            continue
        if turns and turns[-1][0] == speaker:
            turns[-1][1] += " " + said
        else:
            turns.append([speaker, said])
    return [f"{speaker}: {said}" if speaker else said for speaker, said in turns]


def parse_chat(text: str) -> list[str]:
    """Return chat messages as ``Name: message``.

    Zoom has written several layouts over the years, for example
    ``00:01:02\\tName:\\tmessage``, ``00:01:02\\t From  Name : message`` and
    ``00:01:02 From Name to Everyone:`` with the message on the next line.
    A line without a timestamp continues the message before it.
    """
    messages: list[list[str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _CHAT_LINE.match(line)
        if not match:
            if messages:
                messages[-1][1] = f"{messages[-1][1]} {line}".strip()
            else:
                messages.append(["", line])
            continue
        rest = match.group(2).strip()
        if rest.startswith("From "):
            rest = rest[len("From ") :].strip()
        name, sep, message = rest.partition(":")
        if not sep:
            messages.append(["", rest])
            continue
        messages.append([_TO_EVERYONE.sub("", name.strip()), message.strip()])
    return [f"{name}: {message}" if name else message for name, message in messages if message]


# ---------------------------------------------------------------------------
# Rendering: deterministic, no volatile fields.
# ---------------------------------------------------------------------------
def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _display_name(user: dict[str, Any]) -> str:
    name = str(user.get("display_name") or "").strip()
    if name:
        return name
    return " ".join(
        part
        for part in (str(user.get(key) or "").strip() for key in ("first_name", "last_name"))
        if part
    )


def render_meeting(
    meeting: dict[str, Any], transcript: list[str], chat: list[str], host: str = ""
) -> dict[str, Any]:
    topic = str(meeting.get("topic") or "").strip() or "Untitled meeting"
    start = _parse_time(meeting.get("start_time"))
    when = start.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if start else ""
    duration = meeting.get("duration")
    fields = [
        ("Meeting", topic),
        ("Date", when),
        ("Duration", f"{duration} min" if duration else ""),
        ("Host", host),
    ]
    parts = [f"{label}: {value}" for label, value in fields if value]
    if transcript:
        parts.extend(["", "Transcript:", *transcript])
    if chat:
        parts.extend(["", "Chat:", *chat])
    title = f"{topic} ({start.date().isoformat()})" if start else topic
    return {
        "id": f"meeting:{meeting['uuid']}",
        "title": title,
        "content": "\n".join(parts),
        "url": str(meeting.get("share_url") or ""),
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _ZoomConfig:
    user_ids: tuple[str, ...]
    since: date | None
    include_transcripts: bool
    include_chat: bool
    transcript_wait_hours: float

    def text_file_types(self) -> set[str]:
        types = set()
        if self.include_transcripts:
            types.add("TRANSCRIPT")
        if self.include_chat:
            types.add("CHAT")
        return types


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _paginate(client: Any, path: str, params: dict[str, Any], key: str) -> Iterator[dict]:
    token = None
    while True:
        page = client.get(path, {**params, "next_page_token": token} if token else params)
        yield from page.get(key) or []
        next_token = page.get("next_page_token")
        if not next_token:
            return
        if next_token == token:
            raise ZoomAPIError("Zoom pagination did not advance")
        token = next_token


def _resolve_users(client: Any, user_ids: tuple[str, ...]) -> list[dict]:
    if user_ids:
        return [client.get(f"/users/{quote(user_id, safe='@')}") for user_id in user_ids]
    # Deactivated users keep their recordings. Listing only active users would
    # make deactivating someone look like all of their meetings were deleted.
    users: dict[str, dict] = {}
    for status in ("active", "inactive"):
        for user in _paginate(client, "/users", {"status": status}, "users"):
            users.setdefault(user["id"], user)
    return list(users.values())


def _date_slices(start: date, end: date) -> Iterator[tuple[date, date]]:
    while start <= end:
        stop = min(start + timedelta(days=_SLICE_DAYS - 1), end)
        yield start, stop
        start = stop + timedelta(days=1)


def _text_files(meeting: dict, config: _ZoomConfig) -> list[dict]:
    types = config.text_file_types()
    files = [f for f in meeting.get("recording_files") or [] if f.get("file_type") in types]
    return sorted(files, key=lambda f: (str(f.get("recording_start") or ""), str(f.get("id"))))


def _signature(meeting: dict, config: _ZoomConfig) -> str:
    files = [
        [f.get("file_type"), f.get("id"), f.get("status"), f.get("file_size")]
        for f in _text_files(meeting, config)
    ]
    payload = [meeting.get("topic"), meeting.get("start_time"), meeting.get("duration"), files]
    return hashlib.md5(json.dumps(payload, default=str).encode()).hexdigest()


def _is_pending(meeting: dict, config: _ZoomConfig, now: datetime) -> bool:
    files = meeting.get("recording_files") or []
    watched = config.text_file_types() | _MEDIA_FILE_TYPES
    for file in files:
        if file.get("file_type") in watched and file.get("status") not in (None, "completed"):
            return True
    if not config.include_transcripts:
        return False
    has_media = any(f.get("file_type") in _MEDIA_FILE_TYPES for f in files)
    has_transcript = any(f.get("file_type") == "TRANSCRIPT" for f in files)
    if has_transcript or not has_media:
        return False
    start = _parse_time(meeting.get("start_time"))
    if start is None:
        return False
    ended = start + timedelta(minutes=int(meeting.get("duration") or 0))
    return now - ended < timedelta(hours=config.transcript_wait_hours)


def _meeting_row(client: Any, meeting: dict, config: _ZoomConfig, hosts: dict[str, str]) -> dict:
    transcript: list[str] = []
    chat: list[str] = []
    for file in _text_files(meeting, config):
        url = file.get("download_url")
        if not url:
            # On-premise accounts return a file_path instead, which is not reachable here.
            logger.warning("Zoom recording file without download_url skipped.")
            continue
        text = client.download(url)
        if file.get("file_type") == "TRANSCRIPT":
            transcript.extend(parse_vtt(text))
        else:
            chat.extend(parse_chat(text))
    return render_meeting(meeting, transcript, chat, hosts.get(meeting.get("host_id"), ""))


def _iter_rows(
    client: Any,
    config: _ZoomConfig,
    state: dict,
    stats: dict[str, int],
    *,
    now: Callable[[], datetime] | None = None,
) -> Iterator[dict]:
    """Yield changed meetings and tombstones. Pure of dlt, so tests drive it with a dict."""
    current = (now or _utcnow)()
    today = current.date()
    stored = state.get("since")
    since = config.since or (date.fromisoformat(stored) if stored else None)
    since = since or today - timedelta(days=DEFAULT_SINCE_DAYS)

    users = _resolve_users(client, config.user_ids)
    hosts = {user["id"]: _display_name(user) for user in users if user.get("id")}

    # List the whole window before yielding anything: a failure here raises
    # before a single row or tombstone is produced.
    listed: dict[str, dict] = {}
    for user in users:
        path = f"/users/{quote(user['id'], safe='@')}/recordings"
        for start, stop in _date_slices(since, today):
            params = {"from": start.isoformat(), "to": stop.isoformat()}
            for meeting in _paginate(client, path, params, "meetings"):
                if meeting.get("uuid"):
                    listed.setdefault(meeting["uuid"], meeting)

    known: dict[str, str] = state.setdefault("meetings", {})
    for uuid, meeting in listed.items():
        stats["listed"] += 1
        if _is_pending(meeting, config, current):
            stats["pending"] += 1
            continue
        signature = _signature(meeting, config)
        if known.get(uuid) == signature:
            stats["unchanged"] += 1
            continue
        row = _meeting_row(client, meeting, config, hosts)
        known[uuid] = signature
        stats["emitted"] += 1
        yield row

    for uuid in [uuid for uuid in known if uuid not in listed]:
        del known[uuid]
        stats["deleted"] += 1
        yield {"id": f"meeting:{uuid}", "_deleted": True}

    state["since"] = since.isoformat()
    logger.info(
        "Zoom: %d meeting(s) listed, %d emitted, %d pending, %d deleted.",
        stats["listed"],
        stats["emitted"],
        stats["pending"],
        stats["deleted"],
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def _parse_since(since: date | str | None) -> date | None:
    if since is None or isinstance(since, date):
        return since.date() if isinstance(since, datetime) else since
    return date.fromisoformat(since)


def zoom_source(
    account_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    *,
    user_ids: list[str] | None = None,
    since: date | str | None = None,
    include_transcripts: bool = True,
    include_chat: bool = True,
    transcript_wait_hours: float = DEFAULT_TRANSCRIPT_WAIT_HOURS,
    resource_name: str = "zoom_meetings",
    check_active: Callable[[], None] | None = None,
    client: Any = None,
):
    """Return a ``dlt`` resource that yields one document per Zoom cloud recording.

    Hand the result to ``cognee.remember(...)`` with ``write_disposition="merge"``
    and ``primary_key="id"``.

    Args:
        account_id: Server-to-Server OAuth account id (``ZOOM_ACCOUNT_ID``).
        client_id: App client id (``ZOOM_CLIENT_ID``).
        client_secret: App client secret (``ZOOM_CLIENT_SECRET``).
        user_ids: Zoom user ids or emails to sync. Defaults to every active and
            deactivated user of the account.
        since: First day of the ``start_time`` window, as a date or ISO string.
            Defaults to the day stored by the first run, or 30 days back.
        include_transcripts: Ingest the audio transcript (VTT) of each recording.
        include_chat: Ingest the in-meeting chat saved with each recording.
        transcript_wait_hours: How long a recording without a transcript is held
            back before it is ingested without one.
        resource_name: Staging table name. Use a different one per sync scope
            that shares a dataset.
        check_active: Optional host authorization checkpoint, called around each row.
        client: Pre-built client (see :class:`ZoomClient`). Mainly an injection
            point for tests and hosts.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError('The Zoom connector requires dlt: pip install "cognee[dlt]".') from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "Zoom sync requires a cognee build with table-scoped DLT document cleanup."
        )

    if client is None:
        credentials = {
            "ZOOM_ACCOUNT_ID": account_id or os.getenv("ZOOM_ACCOUNT_ID"),
            "ZOOM_CLIENT_ID": client_id or os.getenv("ZOOM_CLIENT_ID"),
            "ZOOM_CLIENT_SECRET": client_secret or os.getenv("ZOOM_CLIENT_SECRET"),
        }
        missing = [name for name, value in credentials.items() if not value]
        if missing:
            raise ValueError(
                f"Zoom credentials missing: pass them explicitly or set {', '.join(missing)}."
            )
        client = ZoomClient(*credentials.values())

    config = _ZoomConfig(
        user_ids=tuple(user_ids or ()),
        since=_parse_since(since),
        include_transcripts=include_transcripts,
        include_chat=include_chat,
        transcript_wait_hours=float(transcript_wait_hours),
    )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def zoom_meetings():
        stats.clear()
        stats.update(listed=0, pending=0, unchanged=0, emitted=0, deleted=0)
        rows = _iter_rows(client, config, dlt.current.resource_state(), stats)
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = zoom_meetings()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "zoom")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
