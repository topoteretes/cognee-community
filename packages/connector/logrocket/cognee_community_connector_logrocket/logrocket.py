"""Bounded LogRocket MCP document source for cognee.

The MCP search tools cap results without data pagination. This source therefore
fails when a response reaches its configured cap, never treats absence as a
deletion, and requires explicit acknowledgement of those limitations.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from cognee_community_connector_logrocket.client import LogRocketMCPClient
from cognee_community_connector_logrocket.redact import redact_text

SOURCE_NAME = "logrocket"
SESSION_RESOURCE = "logrocket_sessions"
ISSUE_RESOURCE = "logrocket_issues"
SUPPORTED_RESOURCES = frozenset({"sessions", "issues"})


class IncompleteLogRocketResultError(RuntimeError):
    """A capped MCP result must not be loaded as a successful import."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _required_string(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"LogRocket record is missing required string field {key!r}.")
    return value


def _optional_string(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"LogRocket field {key!r} must be a string when present.")
    return redact_text(value)


def session_to_row(
    record: dict[str, Any],
    organization_id: str,
    project_id: str,
    *,
    include_user_id: bool = False,
    include_location: bool = False,
) -> dict[str, str]:
    """Validate and render one observed `find_sessions` entry."""
    if not isinstance(record, dict):
        raise ValueError("Every LogRocket session must be an object.")
    recording_id = _required_string(record, "recordingID")
    session_id = record.get("sessionID")
    if not isinstance(session_id, int) or isinstance(session_id, bool):
        raise ValueError("LogRocket sessionID must be an integer.")
    session_url = _required_string(record, "sessionURL")
    date = record.get("date")
    if not _is_number(date):
        raise ValueError("LogRocket session date must be a Unix timestamp in milliseconds.")

    stable_id = f"logrocket:{organization_id}:{project_id}:session:{recording_id}:{session_id}"
    lines = [
        "Type: LogRocket session",
        f"Recorded: {datetime.fromtimestamp(date / 1000, tz=UTC).isoformat()}",
    ]
    duration = record.get("durationMs")
    if duration is not None:
        if not _is_number(duration):
            raise ValueError("LogRocket durationMs must be numeric when present.")
        lines.append(f"Duration milliseconds: {duration}")

    browser = record.get("browserInfo")
    if browser is not None:
        if not isinstance(browser, dict):
            raise ValueError("LogRocket browserInfo must be an object when present.")
        for label, key in (
            ("Browser", "browser"),
            ("Browser version", "browserVersion"),
            ("Operating system", "os"),
            ("Operating system version", "osVersion"),
            ("Device", "deviceName"),
        ):
            value = _optional_string(browser, key)
            if value:
                lines.append(f"{label}: {value}")

    if include_user_id:
        user = record.get("user")
        if user is not None and not isinstance(user, dict):
            raise ValueError("LogRocket user must be an object when present.")
        user_id = _optional_string(user or {}, "userID")
        if user_id:
            lines.append(f"User ID: {user_id}")

    if include_location:
        location = record.get("location")
        if location is not None and not isinstance(location, dict):
            raise ValueError("LogRocket location must be an object when present.")
        # IP is deliberately never emitted, even when coarse location is enabled.
        for label, key in (("Country", "country"), ("Region", "region"), ("City", "city")):
            value = _optional_string(location or {}, key)
            if value:
                lines.append(f"{label}: {value}")

    return {
        "id": stable_id,
        "title": f"LogRocket session {recording_id}/{session_id}",
        "content": "\n".join(lines),
        "url": session_url,
    }


def issue_to_row(record: dict[str, Any], organization_id: str, project_id: str) -> dict[str, str]:
    """Validate and render one observed `find_issues` entry.

    LogRocket exposes no explicit issue ID in the observed response. A hash of
    the source URL is the best available provisional identity.
    """
    if not isinstance(record, dict):
        raise ValueError("Every LogRocket issue must be an object.")
    url = _required_string(record, "url")
    issue_type = _required_string(record, "issueType")
    earliest = _required_string(record, "earliest")
    latest = _required_string(record, "latest")
    event_count = record.get("eventCount")
    if not _is_number(event_count):
        raise ValueError("LogRocket issue eventCount must be numeric.")
    messages = record.get("messages")
    if not isinstance(messages, list):
        raise ValueError("LogRocket issue messages must be an array.")

    # Search response order is not an identity contract. Canonicalize redacted
    # text and prefer an explicit title over message/event text for the heading.
    message_fields: dict[str, set[str]] = {
        key: set() for key in ("title", "message", "eventMessage")
    }
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Every LogRocket issue message must be an object.")
        for key in ("title", "message", "eventMessage"):
            candidate = _optional_string(message, key)
            if candidate:
                message_fields[key].add(candidate)
    title = next((min(values) for values in message_fields.values() if values), "")
    rendered_messages = sorted(set().union(*message_fields.values()))

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    stable_id = f"logrocket:{organization_id}:{project_id}:issue-url-sha256:{digest}"
    lines = [
        "Type: LogRocket issue",
        f"Issue type: {redact_text(issue_type)}",
        f"First observed: {redact_text(earliest)}",
        f"Last observed: {redact_text(latest)}",
        f"Event count: {event_count}",
    ]
    for label, key in (("Platform", "platformType"), ("Triage status", "triageStatus")):
        value = _optional_string(record, key)
        if value:
            lines.append(f"{label}: {value}")
    if rendered_messages:
        lines.extend(["Messages:", *(f"- {message}" for message in rendered_messages)])
    return {
        "id": stable_id,
        "title": title or f"LogRocket {issue_type} issue",
        "content": "\n".join(lines),
        "url": url,
    }


def _run(awaitable):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()
    raise RuntimeError("LogRocket extraction must run in a synchronous dlt worker.")


def _window(lookback_days: int, now: datetime, start: datetime | None, end: datetime | None):
    end = end or now
    start = start or end - timedelta(days=lookback_days)
    for value in (start, end):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("LogRocket window boundaries must include a timezone.")
    if start >= end:
        raise ValueError("LogRocket window_start must precede window_end.")
    return start.astimezone(UTC), end.astimezone(UTC)


def _fetch_rows(
    client: Any,
    resource: str,
    *,
    organization_id: str,
    project_id: str,
    limit: int,
    lookback_days: int,
    window_start: datetime | None,
    window_end: datetime | None,
    query: str | None,
    include_user_id: bool,
    include_location: bool,
    now: datetime | None = None,
):
    """Validate the entire response before yielding; never read or write a cursor.

    A short search result is not evidence that the window was exhausted. Every
    run re-fetches its configured window and emits all returned rows for merge.
    """
    current = now or datetime.now(UTC)
    start, end = _window(lookback_days, current, window_start, window_end)
    if resource == "sessions":
        window_query = f"Sessions recorded between {start.isoformat()} and {end.isoformat()}"
        arguments = {"query": f"{window_query}. {query}" if query else window_query, "limit": limit}
        container = "sessionDefinitions"
    elif resource == "issues":
        arguments = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "limit": limit,
            "severity": "any",
            "triageStatuses": ["untriaged", "highImpact", "lowImpact", "ignored"],
        }
        if query:
            arguments["query"] = query
        container = "issues"
    else:  # pragma: no cover - guarded by factory
        raise ValueError(f"Unsupported LogRocket resource: {resource}")

    response = _run(client.search(f"find_{resource}", arguments))
    if not isinstance(response, dict) or not isinstance(response.get(container), list):
        raise ValueError(f"LogRocket find_{resource} response has an unexpected structure.")
    records = response[container]
    if len(records) >= limit:
        raise IncompleteLogRocketResultError(
            f"find_{resource} returned the configured limit ({limit}); result may be truncated. "
            "Narrow the query or time window before retrying."
        )

    parser = session_to_row if resource == "sessions" else issue_to_row
    rows = [
        parser(
            record,
            organization_id,
            project_id,
            **(
                {"include_user_id": include_user_id, "include_location": include_location}
                if resource == "sessions"
                else {}
            ),
        )
        for record in records
    ]
    yield from rows


def logrocket_source(
    organization_id: str,
    project_id: str,
    *,
    resources: Iterable[str] = ("sessions", "issues"),
    api_key: str | None = None,
    client: Any = None,
    session_query: str | None = None,
    issue_query: str | None = None,
    session_lookback_days: int = 7,
    issue_lookback_days: int = 30,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    limit: int = 100,
    include_user_id: bool = False,
    include_location: bool = False,
    acknowledge_bounded_results: bool = False,
):
    """Return a bounded LogRocket document source for ``cognee.remember``.

    Each run requests the full configured window, without a cursor or hash cache.
    Explicit timezone-aware boundaries allow repeatable historical imports.
    Issue dates filter occurrences, not modification times; re-fetching the
    retained window makes old issues eligible for updated triage and content.
    Search absence is never emitted as a tombstone. Pass this source to
    ``remember_logrocket`` or use ``cognee.remember(..., write_disposition="merge")``.
    """
    if not acknowledge_bounded_results:
        raise ValueError(
            "Set acknowledge_bounded_results=True after accepting that LogRocket MCP search "
            "has no exhaustive data pagination or deletion feed."
        )
    selected = tuple(dict.fromkeys(resources))
    if not selected or any(item not in SUPPORTED_RESOURCES for item in selected):
        raise ValueError("resources must contain sessions, issues, or both.")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100.")
    if any(
        type(days) is not int or days < 1 for days in (session_lookback_days, issue_lookback_days)
    ):
        raise ValueError("Session and issue lookback days must be positive integers.")
    for boundary in (window_start, window_end):
        if boundary is not None and not isinstance(boundary, datetime):
            raise ValueError("LogRocket window boundaries must be datetime objects.")
    # Fail before opening a connection, including on naive or reversed boundaries.
    _window(session_lookback_days, datetime.now(UTC), window_start, window_end)
    try:
        import dlt
        from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ImportError(
            "Install the LogRocket connector with its declared dependencies."
        ) from exc

    search_client = client or LogRocketMCPClient(organization_id, project_id, api_key=api_key)

    def require_merge():
        if dlt.current.resource().write_disposition != "merge":
            raise ValueError(
                "LogRocket bounded imports require write_disposition='merge'. "
                "Use remember_logrocket or pass write_disposition='merge' to Cognee."
            )

    @dlt.resource(name=SESSION_RESOURCE, primary_key="id", write_disposition="merge")
    def sessions():
        require_merge()
        yield from _fetch_rows(
            search_client,
            "sessions",
            organization_id=organization_id,
            project_id=project_id,
            limit=limit,
            lookback_days=session_lookback_days,
            window_start=window_start,
            window_end=window_end,
            query=session_query,
            include_user_id=include_user_id,
            include_location=include_location,
        )

    @dlt.resource(name=ISSUE_RESOURCE, primary_key="id", write_disposition="merge")
    def issues():
        require_merge()
        yield from _fetch_rows(
            search_client,
            "issues",
            organization_id=organization_id,
            project_id=project_id,
            limit=limit,
            lookback_days=issue_lookback_days,
            window_start=window_start,
            window_end=window_end,
            query=issue_query,
            include_user_id=False,
            include_location=False,
        )

    @dlt.source(name=SOURCE_NAME)
    def source():
        available = {"sessions": sessions, "issues": issues}
        return [available[item] for item in selected]

    result = source()
    setattr(result, DOCUMENT_SOURCE_ATTR, SOURCE_NAME)
    return result


async def remember_logrocket(source: Any, *, dataset_name: str = "logrocket", **kwargs: Any):
    """Ingest into a dedicated dataset with merge enforced at the Cognee boundary.

    Cognee otherwise overrides resource hints with its default ``replace``.
    Re-read retained staging rows so absent search results cannot trigger cleanup.
    """
    import cognee
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    if document_source_tag(source) != SOURCE_NAME:
        raise ValueError("remember_logrocket requires a logrocket_source instance.")
    if kwargs.pop("write_disposition", "merge") != "merge":
        raise ValueError("LogRocket bounded imports require write_disposition='merge'.")
    if kwargs.pop("primary_key", "id") != "id":
        raise ValueError("LogRocket imports require primary_key='id'.")
    return await cognee.remember(
        source, dataset_name=dataset_name, write_disposition="merge", primary_key="id", **kwargs
    )
