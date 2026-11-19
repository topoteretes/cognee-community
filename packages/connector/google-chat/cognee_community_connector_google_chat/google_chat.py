"""Read selected Google Chat spaces into Cognee as thread documents."""

from __future__ import annotations

import copy
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("google_chat_connector")

GOOGLE_CHAT_SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
]
_EVENT_RETENTION = timedelta(days=28)
_EVENT_OVERLAP = timedelta(seconds=2)
_MESSAGE_EVENT_FILTER = (
    '(eventTypes:"google.workspace.chat.message.v1.created" OR '
    'eventTypes:"google.workspace.chat.message.v1.updated" OR '
    'eventTypes:"google.workspace.chat.message.v1.deleted")'
)
_MAX_RETRIES = 3


def build_google_chat_service(
    credentials_path: str = "credentials.json",
    token_path: str = "token.json",
) -> Any:
    """Build a Chat v1 client using installed-app, read-only user OAuth."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - depends on optional runtime deps
        raise ImportError(
            "The Google Chat connector requires google-api-python-client, google-auth, "
            "and google-auth-oauthlib. Install the connector package dependencies."
        ) from exc

    credentials = None
    if os.path.exists(token_path):
        credentials = Credentials.from_authorized_user_file(token_path, GOOGLE_CHAT_SCOPES)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not os.path.exists(credentials_path):
                raise FileNotFoundError(
                    f"Google Chat OAuth client credentials not found at {credentials_path!r}. "
                    "Create a Desktop OAuth client in a Google Cloud project with the Chat API "
                    "enabled, then pass its JSON file as credentials_path."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_path, GOOGLE_CHAT_SCOPES
            )
            credentials = flow.run_local_server(port=0)

        descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
            token_file.write(credentials.to_json())
        os.chmod(token_path, 0o600)

    return build("chat", "v1", credentials=credentials, cache_discovery=False)


def google_chat_source(
    space_names: list[str] | tuple[str, ...],
    *,
    credentials_path: str = "credentials.json",
    token_path: str = "token.json",
    service: Any | None = None,
):
    """Create a dlt source for explicitly selected, named Google Chat spaces.

    The source produces one document per thread. A test or an embedding
    application can pass an already-authenticated Google API service as
    ``service``; normal use relies on installed-app OAuth.
    """
    names = _validate_space_names(space_names)
    try:
        import dlt
    except ImportError as exc:  # pragma: no cover - installed with package
        raise ImportError("Google Chat connector requires dlt.") from exc

    @dlt.resource(
        name="google_chat_threads",
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def google_chat_threads():
        api = service or build_google_chat_service(credentials_path, token_path)
        yield from _iter_rows(api, names, dlt.current.resource_state())

    resource = google_chat_threads()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "google_chat")

    @dlt.source(name="google_chat")
    def source():
        return resource

    result = source()
    setattr(result, DOCUMENT_SOURCE_ATTR, "google_chat")
    return result


def _validate_space_names(space_names: list[str] | tuple[str, ...]) -> list[str]:
    if isinstance(space_names, str) or not space_names:
        raise ValueError("Pass at least one named space as a sequence of 'spaces/{space_id}'.")
    names = list(dict.fromkeys(space_names))
    if any(
        not isinstance(name, str)
        or not name.startswith("spaces/")
        or "/" in name[7:]
        for name in names
    ):
        raise ValueError("Every space must be a resource name in the form 'spaces/{space_id}'.")
    return names


def _iter_rows(service: Any, space_names: list[str], state: dict[str, Any]):
    """Preflight all requested spaces, then process each space independently."""
    space_metadata = {}
    for space_name in space_names:
        space = _execute(service.spaces().get(name=space_name))
        if space.get("name") != space_name:
            raise ValueError(f"Google Chat returned an unexpected resource for {space_name!r}.")
        if space.get("spaceType") != "SPACE":
            raise ValueError(
                f"{space_name!r} is not a named Google Chat space; direct messages and "
                "group chats are not supported."
            )
        space_metadata[space_name] = space

    # Stage every space before yielding anything. If any API page fails, neither
    # tombstones nor a partial sync can escape and no cursor or mapping advances.
    next_state = copy.deepcopy(state)
    rows = []

    # A removed space is a user-requested removal from this source's selection.
    # Tombstone its known threads so Cognee can forget them.
    removed_spaces = sorted(set(next_state) - set(space_names))
    for space_name in removed_spaces:
        previous = next_state.get(space_name, {})
        thread_names = set(previous.get("message_threads", {}).values())
        rows.extend(
            _deleted_thread_row(space_name, thread_name)
            for thread_name in sorted(thread_names)
        )
        next_state.pop(space_name, None)

    for space_name in space_names:
        rows.extend(
            _sync_space(service, space_name, next_state, space=space_metadata[space_name])
        )

    for row in rows:
        yield row
    state.clear()
    state.update(next_state)


def _sync_space(
    service: Any,
    space_name: str,
    state: dict[str, Any],
    *,
    now: datetime | None = None,
    space: dict[str, Any] | None = None,
):
    """Synchronize one space; mutate its state only after all required reads finish."""
    sync_time = _as_utc(now or datetime.now(UTC))
    sync_time_text = _format_time(sync_time)
    space = space or _execute(service.spaces().get(name=space_name))
    previous = copy.deepcopy(state.get(space_name, {}))
    cursor = previous.get("cursor")

    if not cursor:
        rows, next_state = _backfill_space(service, space_name, space, sync_time_text)
    elif _cursor_is_stale(cursor, sync_time):
        rows, next_state = _reconcile_space(service, space_name, space, previous, sync_time_text)
    else:
        try:
            rows, next_state = _incremental_space(
                service, space_name, space, previous, sync_time
            )
        except Exception as exc:
            if not _is_expired_cursor_error(exc):
                raise
            rows, next_state = _reconcile_space(
                service, space_name, space, previous, sync_time_text
            )

    # Rows are fully prepared before emission. State is advanced only after the
    # consumer takes every row; dlt commits the resource state with the pipeline.
    for row in rows:
        yield row
    state[space_name] = next_state


def _backfill_space(service, space_name, space, cursor):
    messages = _list_messages(service, space_name, show_deleted=True)
    groups = _group_live_messages(messages)
    message_threads = _message_thread_map(groups)
    rows = [
        _thread_to_row(space_name, thread_name, thread_messages, space=space)
        for thread_name, thread_messages in sorted(groups.items())
    ]
    return rows, {"cursor": cursor, "message_threads": message_threads}


def _reconcile_space(service, space_name, space, previous, cursor):
    messages = _list_messages(service, space_name, show_deleted=True)
    groups = _group_live_messages(messages)
    message_threads = _message_thread_map(groups)
    previous_threads = set(previous.get("message_threads", {}).values())
    current_threads = set(groups)

    rows = [
        _thread_to_row(space_name, thread_name, thread_messages, space=space)
        for thread_name, thread_messages in sorted(groups.items())
    ]
    rows.extend(
        _deleted_thread_row(space_name, thread_name)
        for thread_name in sorted(previous_threads - current_threads)
    )
    return rows, {"cursor": cursor, "message_threads": message_threads}


def _incremental_space(service, space_name, space, previous, now):
    cursor = _parse_time(previous["cursor"])
    end_time = _format_time(now)
    start_time = _format_time(cursor - _EVENT_OVERLAP)
    event_filter = (
        f'{_MESSAGE_EVENT_FILTER} AND startTime="{start_time}" AND endTime="{end_time}"'
    )

    events = _list_events(service, space_name, event_filter)
    parsed = _messages_from_event(events)
    old_map = dict(previous.get("message_threads", {}))
    new_map = dict(old_map)
    affected_threads: set[str] = set()

    for item in parsed:
        message_data = item["message"]
        message_name = message_data.get("name")
        if not message_name:
            continue
        if item["event_type"] == "deleted":
            thread_name = old_map.get(message_name)
            if thread_name:
                affected_threads.add(thread_name)
            new_map.pop(message_name, None)
            continue
        thread_name = _thread_name(message_data)
        new_map[message_name] = thread_name
        affected_threads.add(thread_name)

    rows = []
    for thread_name in sorted(affected_threads):
        thread_messages = _list_messages(
            service,
            space_name,
            show_deleted=True,
            filter=f"thread.name = {thread_name}",
        )
        live_messages = [
            message
            for message in thread_messages
            if not message.get("deleteTime")
            and (message.get("text") or message.get("attachments"))
        ]
        for message_name, mapped_thread in list(new_map.items()):
            if mapped_thread == thread_name:
                del new_map[message_name]
        for message in live_messages:
            message_name = message.get("name")
            if message_name:
                new_map[message_name] = thread_name
        if live_messages:
            rows.append(_thread_to_row(space_name, thread_name, live_messages, space=space))
        elif thread_name in set(old_map.values()):
            rows.append(_deleted_thread_row(space_name, thread_name))

    # The fixed endTime, not the last returned event time, is the upper bound
    # proved complete by the finished page walk.
    return rows, {"cursor": end_time, "message_threads": new_map}


def _list_messages(service, space_name, *, show_deleted, filter=None):
    messages = []
    page_token = None
    while True:
        request = service.messages().list(
            parent=space_name,
            pageSize=1000,
            pageToken=page_token,
            showDeleted=show_deleted,
            **({"filter": filter} if filter else {}),
        )
        response = _execute(request)
        messages.extend(response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return messages


def _list_events(service, space_name, filter):
    events = []
    page_token = None
    seen_names = set()
    while True:
        request = service.spaceEvents().list(
            parent=space_name,
            filter=filter,
            pageSize=1000,
            pageToken=page_token,
        )
        response = _execute(request)
        for event in response.get("spaceEvents", []):
            event_name = event.get("name")
            if event_name and event_name in seen_names:
                continue
            if event_name:
                seen_names.add(event_name)
            events.append(event)
        page_token = response.get("nextPageToken")
        if not page_token:
            return events


def _messages_from_event(events):
    """Unpack single and server-included batch message events."""
    result = []
    for event in events:
        event_type = event.get("eventType", "")
        if not event_type.startswith("google.workspace.chat.message.v1."):
            continue
        suffix = event_type.rsplit(".", 1)[-1]
        is_batch = suffix.startswith("batch")
        action = suffix.removeprefix("batch").lower()
        if is_batch:
            payload_key = f"messageBatch{suffix.removeprefix('batch').title()}EventData"
        else:
            payload_key = f"message{action.title()}EventData"
        payload = event.get(payload_key, {}) or {}
        messages = payload.get("messages", []) if is_batch else [payload]
        for item in messages:
            message_data = item.get("message") if isinstance(item, dict) else None
            if isinstance(message_data, dict):
                result.append({"event_type": action, "message": message_data})
    return result


def _group_live_messages(messages):
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for message_data in messages:
        if message_data.get("deleteTime"):
            continue
        if not message_data.get("text") and not message_data.get("attachments"):
            logger.info("Skipping textless Google Chat message %s.", message_data.get("name"))
            continue
        groups[_thread_name(message_data)].append(message_data)
    return dict(groups)


def _message_thread_map(groups):
    return {
        message_data["name"]: thread_name
        for thread_name, messages in groups.items()
        for message_data in messages
        if message_data.get("name")
    }


def _thread_name(message_data):
    thread_name = (message_data.get("thread") or {}).get("name")
    return thread_name or message_data.get("name")


def _thread_to_row(space_name, thread_name, messages, *, space=None):
    ordered = sorted(messages, key=lambda message_data: _parse_time(message_data.get("createTime")))
    space_title = (space or {}).get("displayName") or space_name
    lines = [f"Space: {space_title}", f"Thread: {thread_name}"]
    for message_data in ordered:
        sender = message_data.get("sender") or {}
        sender_name = sender.get("displayName") or sender.get("name") or "Unknown sender"
        timestamp = message_data.get("createTime") or "unknown time"
        text = (message_data.get("text") or "").strip()
        line = f"[{timestamp}] {sender_name}: {text}" if text else f"[{timestamp}] {sender_name}:"
        lines.append(line)
        for attachment in message_data.get("attachments") or []:
            attachment_data = attachment.get("attachmentDataRef") or {}
            attachment_name = (
                attachment.get("name") or attachment_data.get("resourceName") or "attachment"
            )
            mime_type = attachment.get("contentType") or "unknown type"
            lines.append(f"  Attachment: {attachment_name} ({mime_type})")
    return {
        "id": thread_name,
        "space_name": space_name,
        "space_display_name": space_title,
        "thread_name": thread_name,
        "content": "\n".join(lines),
        "url": next((item.get("permalink") for item in ordered if item.get("permalink")), None),
        "last_update_time": max(
            (
                item.get("lastUpdateTime") or item.get("createTime") or ""
                for item in ordered
            ),
            key=_parse_time,
            default="",
        ),
        "_deleted": False,
    }


def _deleted_thread_row(space_name, thread_name):
    return {
        "id": thread_name,
        "space_name": space_name,
        "thread_name": thread_name,
        "_deleted": True,
    }


def _cursor_is_stale(cursor, now):
    cursor_time = _parse_time(cursor)
    current_time = _as_utc(now)
    return cursor_time > current_time or current_time - cursor_time >= (
        _EVENT_RETENTION - _EVENT_OVERLAP
    )


def _is_expired_cursor_error(exc):
    status = getattr(getattr(exc, "resp", None), "status", None)
    message = str(exc).lower()
    return status == 400 and (
        "starttime" in message or "28 days" in message or "too old" in message
    )


def _execute(request):
    """Execute a generated Google API request with bounded client retries."""
    try:
        return request.execute(num_retries=_MAX_RETRIES)
    except TypeError as exc:
        # Small fake request objects used in tests need not emulate the client
        # library's optional num_retries argument.
        if "num_retries" not in str(exc):
            raise
        return request.execute()


def _parse_time(value):
    if isinstance(value, datetime):
        return _as_utc(value)
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    return _as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _as_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value):
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")
