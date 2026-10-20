"""Cognee dlt source backed by LogRocket's official MCP server.

The connector deliberately uses the public MCP tools ``find_sessions`` and
``find_issues`` instead of undocumented LogRocket REST endpoints. Session
replays are never downloaded: only the structured metadata returned by the MCP
server is converted into documents.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("logrocket_connector")

LOGROCKET_SOURCE_NAME = "logrocket"
LOGROCKET_MCP_URL = "https://mcp.logrocket.com/mcp"
SESSION_TABLE_NAME = "logrocket_sessions"
ISSUE_TABLE_NAME = "logrocket_issues"
_MAX_RETRIES = 5
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
_MISSING = object()


class LogRocketMCPError(RuntimeError):
    """Raised when the LogRocket MCP server returns an unusable response."""


class LogRocketMCPClient:
    """Small synchronous client for MCP Streamable HTTP.

    dlt resources are synchronous generators, so this client intentionally uses
    httpx directly rather than introducing an event-loop boundary into dlt.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = LOGROCKET_MCP_URL,
        timeout: float = 30.0,
        http_client: Any = None,
    ) -> None:
        if not api_key:
            raise ValueError("LogRocket API key required: pass api_key= or set LOGROCKET_API_KEY.")
        self._api_key = api_key
        self._base_url = base_url
        self._http = http_client or httpx.Client(timeout=timeout)
        self._session_id: str | None = None
        self._request_id = 0
        self._initialized = False
        self._tools: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        close = getattr(self._http, "close", None)
        if close:
            close()

    def initialize(self) -> None:
        if self._initialized:
            return

        result = self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "cognee-community-connector-logrocket", "version": "0.1.0"},
            },
        )
        if not isinstance(result, Mapping) or "serverInfo" not in result:
            raise LogRocketMCPError("LogRocket MCP initialize response is missing serverInfo.")
        self._request("notifications/initialized", None, notification=True)
        tools = self._request("tools/list", {})
        listed_tools = tools.get("tools") if isinstance(tools, Mapping) else None
        if not isinstance(listed_tools, list):
            raise LogRocketMCPError("LogRocket MCP tools/list response is missing tools.")
        self._tools = {
            tool["name"]: tool
            for tool in listed_tools
            if isinstance(tool, Mapping) and isinstance(tool.get("name"), str)
        }
        self._initialized = True

    def iter_tool_records(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        record_keys: tuple[str, ...],
    ) -> Iterator[dict[str, Any]]:
        self.initialize()
        tool = self._tools.get(tool_name)
        if tool is None:
            raise LogRocketMCPError(
                f"LogRocket MCP tool {tool_name!r} is unavailable. "
                "Check the configured project and MCP toolset."
            )

        current_arguments = dict(arguments)
        cursor_key = _cursor_argument(tool)
        seen_cursors: set[str] = set()
        while True:
            result = self._request(
                "tools/call", {"name": tool_name, "arguments": current_arguments}
            )
            payload = _tool_payload(result)
            records = _records_from_payload(payload, record_keys)
            for record in records:
                if not isinstance(record, Mapping):
                    raise LogRocketMCPError(f"LogRocket {tool_name} returned a non-object record.")
                yield dict(record)

            next_cursor = _next_cursor(payload)
            if not next_cursor:
                return
            if not cursor_key:
                raise LogRocketMCPError(
                    f"LogRocket {tool_name} returned a cursor but its schema has no "
                    "cursor argument."
                )
            if next_cursor in seen_cursors:
                raise LogRocketMCPError(
                    f"LogRocket {tool_name} returned a repeated pagination cursor."
                )
            seen_cursors.add(next_cursor)
            current_arguments[cursor_key] = next_cursor

    def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        *,
        notification: bool = False,
    ) -> Any:
        self._request_id += 1
        request: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            request["id"] = self._request_id
        if params is not None:
            request["params"] = params

        for attempt in range(_MAX_RETRIES):
            headers = {
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
            try:
                response = self._http.post(self._base_url, headers=headers, json=request)
            except httpx.HTTPError:
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt)
                continue

            if response.status_code in _TRANSIENT_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                time.sleep(_retry_after(response, attempt))
                continue
            if response.status_code >= 400:
                raise LogRocketMCPError(
                    f"LogRocket MCP request {method!r} failed with HTTP {response.status_code}."
                )
            if notification or response.status_code in (202, 204):
                return None

            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self._session_id = session_id
            message = _parse_response(response)
            if isinstance(message, Mapping) and message.get("error"):
                error = message["error"]
                raise LogRocketMCPError(f"LogRocket MCP {method} failed: {error}")
            if not isinstance(message, Mapping) or "result" not in message:
                raise LogRocketMCPError(f"LogRocket MCP {method} returned no result.")
            return message["result"]

        raise AssertionError("retry loop must return or raise")


def logrocket_source(
    *,
    api_key: str | None = None,
    organization_id: str,
    project_id: str,
    resources: tuple[str, ...] = ("sessions", "issues"),
    start_time: datetime | int | str | None = None,
    end_time: datetime | int | str | None = None,
    session_filters: Mapping[str, Any] | None = None,
    issue_filters: Mapping[str, Any] | None = None,
    base_url: str = LOGROCKET_MCP_URL,
    timeout: float = 30.0,
    client: LogRocketMCPClient | None = None,
):
    """Create a dlt source for LogRocket session metadata and issue reports.

    ``start_time`` and ``end_time`` are mapped to the fields advertised by the
    live MCP tool schema. For provider-specific filters, pass ``session_filters``
    or ``issue_filters``; these are forwarded without guessing their names.
    The selected time window is the authoritative snapshot scope for each
    resource, which is what makes upstream disappearance safe to reconcile.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The LogRocket connector requires dlt and httpx. Install this package "
            "with: uv pip install cognee-community-connector-logrocket"
        ) from exc

    api_key = api_key or os.environ.get("LOGROCKET_API_KEY")
    if not api_key:
        raise ValueError("LogRocket API key required: pass api_key= or set LOGROCKET_API_KEY.")
    if not organization_id or not project_id:
        raise ValueError("organization_id and project_id are required.")
    selected = tuple(dict.fromkeys(resources))
    invalid = set(selected) - {"sessions", "issues"}
    if invalid or not selected:
        raise ValueError("resources must contain one or both of: sessions, issues.")

    mcp_client = client or LogRocketMCPClient(api_key, base_url=base_url, timeout=timeout)

    def tool_arguments(tool_name: str, filters: Mapping[str, Any] | None) -> dict[str, Any]:
        tool = mcp_client.tool_schema(tool_name)
        args = _scope_arguments(tool, organization_id, project_id)
        args.update(filters or {})
        _add_time_window(args, tool, start_time, end_time)
        return args

    @dlt.resource(name=SESSION_TABLE_NAME, primary_key="id", write_disposition="replace")
    def sessions():
        args = tool_arguments("find_sessions", session_filters)
        for session in mcp_client.iter_tool_records("find_sessions", args, ("sessions", "results")):
            yield _session_to_row(session, organization_id, project_id)

    @dlt.resource(name=ISSUE_TABLE_NAME, primary_key="id", write_disposition="replace")
    def issues():
        args = tool_arguments("find_issues", issue_filters)
        for issue in mcp_client.iter_tool_records("find_issues", args, ("issues", "results")):
            yield _issue_to_row(issue, organization_id, project_id)

    @dlt.source(name=LOGROCKET_SOURCE_NAME)
    def _logrocket():
        selected_resources = []
        if "sessions" in selected:
            selected_resources.append(sessions)
        if "issues" in selected:
            selected_resources.append(issues)
        return selected_resources

    source = _logrocket()
    setattr(source, DOCUMENT_SOURCE_ATTR, LOGROCKET_SOURCE_NAME)
    return source


def _session_to_row(
    session: Mapping[str, Any], organization_id: str, project_id: str
) -> dict[str, Any]:
    session_id = _first(session, "id", "sessionId", "session_id", "recordingId", "recordingID")
    if not session_id:
        raise LogRocketMCPError("LogRocket session is missing a stable identifier.")
    url = _first(session, "url", "sessionUrl", "session_url")
    title = _first(session, "title", "name") or f"LogRocket session {session_id}"
    content = _document_text(title, session, exclude=("replay", "events", "recording"))
    metadata = {
        key: value for key, value in session.items() if key not in ("replay", "events", "recording")
    }
    return {
        "id": f"{organization_id}/{project_id}/session/{session_id}",
        "title": str(title),
        "content": content,
        "url": url,
        "session_id": str(session_id),
        "started_at": _first(session, "startedAt", "startTime", "started_at", "timestamp"),
        "metadata": _json_text(metadata),
    }


def _issue_to_row(
    issue: Mapping[str, Any], organization_id: str, project_id: str
) -> dict[str, Any]:
    issue_id = _first(issue, "id", "issueId", "issue_id")
    if not issue_id:
        raise LogRocketMCPError("LogRocket issue is missing a stable identifier.")
    title = _first(issue, "title", "name", "label") or f"LogRocket issue {issue_id}"
    content = _document_text(title, issue, exclude=())
    return {
        "id": f"{organization_id}/{project_id}/issue/{issue_id}",
        "title": str(title),
        "content": content,
        "url": _first(issue, "url", "issueUrl", "issue_url"),
        "issue_id": str(issue_id),
        "issue_type": _first(issue, "issueType", "type", "issue_type"),
        "severity": _first(issue, "severity", "priority"),
        "status": _first(issue, "status", "triageStatus", "triage_status"),
        "occurred_at": _first(issue, "firstSeen", "firstDetected", "createdAt", "occurred_at"),
        "metadata": _json_text(issue),
    }


def _document_text(title: Any, record: Mapping[str, Any], exclude: tuple[str, ...]) -> str:
    body = {key: value for key, value in record.items() if key not in exclude}
    return f"# {title}\n\n{_json_text(body)}"


def _first(record: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key, _MISSING)
        if value not in (_MISSING, None, ""):
            return value
    return None


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _parse_response(response: Any) -> Mapping[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        messages = []
        for line in response.text.splitlines():
            if line.startswith("data:"):
                messages.append(json.loads(line[5:].strip()))
        if not messages:
            raise LogRocketMCPError("LogRocket MCP returned an empty event stream.")
        return messages[-1]
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise LogRocketMCPError("LogRocket MCP returned invalid JSON.") from exc


def _tool_payload(result: Any) -> Any:
    if not isinstance(result, Mapping):
        raise LogRocketMCPError("LogRocket MCP tool result is not an object.")
    if result.get("isError"):
        raise LogRocketMCPError(f"LogRocket MCP tool failed: {result.get('content')}")
    if isinstance(result.get("structuredContent"), (Mapping, list)):
        return result["structuredContent"]
    for item in result.get("content", []) or []:
        if isinstance(item, Mapping) and item.get("type") == "text":
            try:
                return json.loads(item.get("text", ""))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise LogRocketMCPError("LogRocket MCP tool returned non-JSON text.") from exc
    raise LogRocketMCPError("LogRocket MCP tool result contains no structured content.")


def _records_from_payload(payload: Any, record_keys: tuple[str, ...]) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        raise LogRocketMCPError("LogRocket MCP tool payload is not a list or object.")
    for key in record_keys:
        if isinstance(payload.get(key), list):
            return payload[key]
    raise LogRocketMCPError(f"LogRocket MCP tool payload has no records ({record_keys}).")


def _next_cursor(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("nextCursor", "next_cursor", "nextPageToken", "next_page_token"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _cursor_argument(tool: Mapping[str, Any]) -> str | None:
    properties = _tool_properties(tool)
    for key in ("cursor", "pageToken", "page_token", "nextCursor", "next_cursor"):
        if key in properties:
            return key
    return None


def _tool_properties(tool: Mapping[str, Any]) -> Mapping[str, Any]:
    schema = tool.get("inputSchema")
    if not isinstance(schema, Mapping) or not isinstance(schema.get("properties"), Mapping):
        return {}
    return schema["properties"]


def _scope_arguments(
    tool: Mapping[str, Any], organization_id: str, project_id: str
) -> dict[str, Any]:
    properties = _tool_properties(tool)
    organization_key = next(
        (
            key
            for key in ("organizationId", "organizationID", "orgId", "orgID", "organization_id")
            if key in properties
        ),
        None,
    )
    project_key = next(
        (
            key
            for key in ("projectId", "projectID", "appId", "appID", "project_id", "app_id")
            if key in properties
        ),
        None,
    )
    if not organization_key or not project_key:
        raise LogRocketMCPError(
            f"LogRocket MCP tool {tool.get('name', '<unknown>')} does not advertise "
            "organization and project scope fields."
        )
    return {organization_key: organization_id, project_key: project_id}


def _add_time_window(
    arguments: dict[str, Any],
    tool: Mapping[str, Any],
    start_time: datetime | int | str | None,
    end_time: datetime | int | str | None,
) -> None:
    if start_time is None and end_time is None:
        return
    properties = _tool_properties(tool)
    if "timeRange" in properties or "time_range" in properties:
        key = "timeRange" if "timeRange" in properties else "time_range"
        arguments[key] = {
            "startMs": _timestamp_ms(start_time) if start_time is not None else None,
            "endMs": _timestamp_ms(end_time) if end_time is not None else None,
        }
        return

    for value, aliases in (
        (
            start_time,
            (
                "startTime",
                "start_time",
                "fromTime",
                "from_time",
                "startDate",
                "start_date",
                "startMs",
            ),
        ),
        (end_time, ("endTime", "end_time", "toTime", "to_time", "endDate", "end_date", "endMs")),
    ):
        if value is None:
            continue
        key = next((candidate for candidate in aliases if candidate in properties), None)
        if key is None:
            raise LogRocketMCPError(
                f"LogRocket MCP tool {tool.get('name', '<unknown>')} does not advertise "
                "a time-window field."
            )
        arguments[key] = _timestamp_ms(value) if key.endswith("Ms") else _timestamp_iso(value)


def _timestamp_ms(value: datetime | int | str) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, datetime):
        point = value if value.tzinfo else value.replace(tzinfo=UTC)
        return int(point.timestamp() * 1000)
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _timestamp_iso(value: datetime | int | str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
    point = value if value.tzinfo else value.replace(tzinfo=UTC)
    return point.isoformat()


def _retry_after(response: Any, attempt: int) -> float:
    value = response.headers.get("Retry-After")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(2**attempt)
