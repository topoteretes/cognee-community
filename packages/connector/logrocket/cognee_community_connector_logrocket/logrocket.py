"""Bounded LogRocket MCP document source for cognee.

The MCP search tools cap results without data pagination. This source therefore
fails when a response reaches its configured cap, never treats absence as a
deletion, and requires explicit acknowledgement of those limitations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MethodType
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from cognee_community_connector_logrocket.client import LogRocketMCPClient
from cognee_community_connector_logrocket.redact import redact_text

SOURCE_NAME = "logrocket"
SESSION_RESOURCE = "logrocket_sessions"
ISSUE_RESOURCE = "logrocket_issues"
SUPPORTED_RESOURCES = frozenset({"sessions", "issues"})
RESOURCE_NAMES = {SESSION_RESOURCE: "sessions", ISSUE_RESOURCE: "issues"}


class IncompleteLogRocketResultError(RuntimeError):
    """A capped MCP result must not be loaded as a successful import."""


def _is_number(value: Any) -> bool:
    try:
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    except OverflowError:
        return False


def _provenance_url(record: dict[str, Any], key: str) -> str:
    """Reject unsafe provenance rather than rewrite an identity-bearing URL."""
    value = _required_string(record, key)
    try:
        parsed = urlsplit(value)
        unsafe = (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or any(ord(char) <= 32 for char in value)
            or redact_text(unquote(value)) != unquote(value)
        )
        sensitive = {
            "token",
            "accesstoken",
            "apikey",
            "authorization",
            "password",
            "secret",
            "email",
            "userid",
        }
        parameters = [
            *parse_qsl(parsed.query, keep_blank_values=True),
            *parse_qsl(parsed.fragment, keep_blank_values=True),
        ]
        unsafe |= any(
            name.lower().replace("_", "").replace("-", "") in sensitive for name, _ in parameters
        )
        decoded_components = [
            component for name, value in parameters for component in (name, value)
        ]
        unsafe |= any(redact_text(component) != component for component in decoded_components)
    except ValueError:
        unsafe = True
    if unsafe:
        raise ValueError(f"LogRocket {key} must be a safe HTTPS provenance URL; value withheld.")
    return value


def _session_date(record: dict[str, Any]) -> datetime:
    value = record.get("date")
    if _is_number(value):
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        except (ValueError, OverflowError, OSError):
            pass
    raise ValueError("LogRocket session date must be a valid Unix timestamp in milliseconds.")


def _destination_identity(pipeline: Any) -> str:
    """Return a credential-free identity for the physical destination and dataset."""
    try:
        location = pipeline.destination_client().config.data_location()
        if not isinstance(location, str) or not location:
            raise ValueError
    except Exception:
        raise ValueError(
            "Cannot determine LogRocket destination ownership; details withheld."
        ) from None
    identity = json.dumps([location, pipeline.dataset_name], separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _bind_dataset(pipeline: Any, scope: dict[str, Any], destination_identity: str) -> None:
    """Reserve ownership outside the pipeline folder that DLT can reset.

    SQLite serializes competing first-use reservations. Keep this registry with
    the DLT state directory; it contains scope metadata, never source records.
    """
    path = Path(pipeline.pipelines_dir) / "logrocket_ownership.sqlite3"
    value = json.dumps(scope, sort_keys=True)
    connection = sqlite3.connect(path, timeout=10)
    try:
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS destination_owners "
                "(destination TEXT PRIMARY KEY, scope TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO destination_owners VALUES (?, ?)",
                (destination_identity, value),
            )
            previous = connection.execute(
                "SELECT scope FROM destination_owners WHERE destination = ?",
                (destination_identity,),
            ).fetchone()[0]
            if previous != value:
                raise ValueError(
                    "LogRocket dataset belongs to a different project or resource selection."
                )
    finally:
        connection.close()


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


def _canonical_identifier(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"LogRocket {name} must be a string.")
    canonical = value.strip()
    if not canonical:
        raise ValueError(f"LogRocket {name} must not be empty.")
    return canonical


def _require_boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"LogRocket {name} must be a boolean.")
    return value


def _selected_resources(source: Any) -> tuple[str, ...]:
    try:
        return tuple(sorted(RESOURCE_NAMES[name] for name in source.resources.selected))
    except (AttributeError, KeyError):
        raise ValueError("LogRocket source has an invalid resource selection.") from None


def _default_dataset_name(source: Any) -> str:
    try:
        scope = (
            source._logrocket_organization,
            source._logrocket_project,
            _selected_resources(source),
        )
    except AttributeError:
        raise ValueError("LogRocket source metadata is missing.") from None
    return f"logrocket_{hashlib.sha256(repr(scope).encode()).hexdigest()[:16]}"


def session_to_row(
    record: dict[str, Any],
    organization_id: str,
    project_id: str,
    *,
    include_user_id: bool = False,
    include_location: bool = False,
) -> dict[str, str]:
    """Validate and render one observed `find_sessions` entry."""
    organization_id = _canonical_identifier(organization_id, "organization_id")
    project_id = _canonical_identifier(project_id, "project_id")
    include_user_id = _require_boolean(include_user_id, "include_user_id")
    include_location = _require_boolean(include_location, "include_location")
    if not isinstance(record, dict):
        raise ValueError("Every LogRocket session must be an object.")
    recording_id = _required_string(record, "recordingID")
    if redact_text(recording_id) != recording_id:
        raise ValueError("LogRocket recordingID contains sensitive text; value withheld.")
    session_id = record.get("sessionID")
    if not isinstance(session_id, int) or isinstance(session_id, bool):
        raise ValueError("LogRocket sessionID must be an integer.")
    session_url = _provenance_url(record, "sessionURL")
    date = _session_date(record)

    stable_id = f"logrocket:{organization_id}:{project_id}:session:{recording_id}:{session_id}"
    lines = [
        "Type: LogRocket session",
        f"Recorded: {date.isoformat()}",
    ]
    duration = record.get("durationMs")
    if duration is not None:
        if not _is_number(duration) or duration < 0:
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
    organization_id = _canonical_identifier(organization_id, "organization_id")
    project_id = _canonical_identifier(project_id, "project_id")
    if not isinstance(record, dict):
        raise ValueError("Every LogRocket issue must be an object.")
    url = _provenance_url(record, "url")
    issue_type = _required_string(record, "issueType")
    earliest = _required_string(record, "earliest")
    latest = _required_string(record, "latest")
    event_count = record.get("eventCount")
    if not _is_number(event_count) or event_count < 0 or event_count % 1:
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
        "title": title or f"LogRocket {redact_text(issue_type)} issue",
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
    if resource == "sessions" and any(
        not start <= _session_date(record) < end for record in records
    ):
        raise ValueError("LogRocket returned sessions outside the requested [start, end) window.")
    unique: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["id"] in unique and unique[row["id"]] != row:
            raise ValueError(
                "LogRocket returned conflicting versions of the same record; values withheld."
            )
        unique[row["id"]] = row
    yield from unique.values()


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
    acknowledge_bounded_results = _require_boolean(
        acknowledge_bounded_results, "acknowledge_bounded_results"
    )
    if not acknowledge_bounded_results:
        raise ValueError(
            "Set acknowledge_bounded_results=True after accepting that LogRocket MCP search "
            "has no exhaustive data pagination or deletion feed."
        )
    organization_id = _canonical_identifier(organization_id, "organization_id")
    project_id = _canonical_identifier(project_id, "project_id")
    include_user_id = _require_boolean(include_user_id, "include_user_id")
    include_location = _require_boolean(include_location, "include_location")
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
        # Cognee reuses a pipeline name across datasets. Bind each dataset
        # separately so importing another project into its own dataset works.
        bindings = dlt.current.source_state().setdefault("logrocket_scopes", {})
        destination_identity = _destination_identity(dlt.current.pipeline())
        active_resources = _selected_resources(dlt.current.source())
        scope = {
            "organization": organization_id,
            "project": project_id,
            "resources": list(active_resources),
        }
        previous = bindings.get(destination_identity)
        if previous is not None and previous != scope:
            raise ValueError(
                "LogRocket dataset belongs to a different project or resource selection."
            )
        _bind_dataset(dlt.current.pipeline(), scope, destination_identity)
        bindings[destination_identity] = scope

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
    metadata = {
        DOCUMENT_SOURCE_ATTR: SOURCE_NAME,
        "_logrocket_organization": organization_id,
        "_logrocket_project": project_id,
    }

    def attach_metadata(source: Any) -> None:
        original_clone = source.clone

        def clone_with_metadata(source: Any, with_name: str | None = None):
            cloned = original_clone(with_name=with_name)
            attach_metadata(cloned)
            return cloned

        for key, value in metadata.items():
            setattr(source, key, value)
        source.clone = MethodType(clone_with_metadata, source)

    attach_metadata(result)
    return result


async def remember_logrocket(source: Any, *, dataset_name: str | None = None, **kwargs: Any):
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
        source,
        dataset_name=dataset_name or _default_dataset_name(source),
        write_disposition="merge",
        primary_key="id",
        **kwargs,
    )
