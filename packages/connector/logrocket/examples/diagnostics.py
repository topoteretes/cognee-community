"""Read-only LogRocket diagnostics. Run from the checkout; not part of the package API."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import httpx2
from jsonschema import validators
from jsonschema.exceptions import SchemaError, ValidationError

from cognee_community_connector_logrocket.logrocket import (
    TRANSIENT_STATUS,
    LogRocketMCPError,
    _collect_tools,
    _CredentialError,
    _exception_type_names,
    _resolve_api_key,
    _retry_after,
)
from cognee_community_connector_logrocket.logrocket import (
    _LogRocketMCPClient as _RuntimeMCPClient,
)


@dataclass(frozen=True)
class _ToolDefinition:
    """The parts of an MCP tool definition needed by the capability spike."""

    name: str
    description: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> _ToolDefinition:
        if not isinstance(value, dict):
            raise ValueError("Every captured MCP tool must be an object.")
        input_schema = value.get("input_schema", value.get("inputSchema"))
        output_schema = value.get("output_schema", value.get("outputSchema"))
        if not isinstance(value.get("name"), str) or not isinstance(input_schema, dict):
            raise ValueError("Every captured MCP tool needs a name and object input schema.")
        if output_schema is not None and not isinstance(output_schema, dict):
            raise ValueError("A captured MCP output schema must be an object when present.")
        description = value.get("description")
        return cls(
            name=value["name"],
            description=description if isinstance(description, str) else None,
            input_schema=input_schema,
            output_schema=output_schema,
        )


@dataclass(frozen=True)
class _CapabilitySnapshot:
    """Lossless-enough capture of MCP discovery and all ``tools/list`` pages."""

    endpoint: str
    protocol_version: str | None
    server_info: dict[str, Any] | None
    server_capabilities: dict[str, Any]
    tools: tuple[_ToolDefinition, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_version": 1,
            "endpoint": self.endpoint,
            "protocol_version": self.protocol_version,
            "server_info": self.server_info,
            "server_capabilities": self.server_capabilities,
            "tools": [tool.to_dict() for tool in self.tools],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> _CapabilitySnapshot:
        if value.get("snapshot_version", 1) != 1:
            raise ValueError("Unsupported capability snapshot version.")
        tools = value.get("tools")
        if not isinstance(tools, list):
            raise ValueError("Capability snapshot must contain a tools array.")
        capabilities = value.get("server_capabilities") or {}
        if not isinstance(capabilities, dict):
            raise ValueError("server_capabilities must be an object.")
        server_info = value.get("server_info")
        if server_info is not None and not isinstance(server_info, dict):
            raise ValueError("server_info must be an object when present.")
        endpoint = value.get("endpoint", "offline-snapshot")
        protocol_version = value.get("protocol_version")
        return cls(
            endpoint=endpoint if isinstance(endpoint, str) else "offline-snapshot",
            protocol_version=protocol_version if isinstance(protocol_version, str) else None,
            server_info=server_info,
            server_capabilities=capabilities,
            tools=tuple(_ToolDefinition.from_dict(tool) for tool in tools),
        )


def _model_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", by_alias=True, exclude_none=True)
    return {"value": str(value)}


async def _collect_snapshot(mcp_client: Any, endpoint: str) -> _CapabilitySnapshot:
    """Capture discovery metadata for development reports, outside ingestion."""
    tools = tuple(
        _ToolDefinition(tool.name, tool.description, tool.input_schema, tool.output_schema)
        for tool in await _collect_tools(mcp_client)
    )
    server_info_value = getattr(mcp_client, "server_info", None)
    return _CapabilitySnapshot(
        endpoint=endpoint,
        protocol_version=getattr(mcp_client, "protocol_version", None),
        server_info=_model_dict(server_info_value) if server_info_value is not None else None,
        server_capabilities=_model_dict(getattr(mcp_client, "server_capabilities", None)),
        tools=tools,
    )


INSPECTION_TIMEOUT = 60


class _LogRocketMCPClient(_RuntimeMCPClient):
    """Development-only inspection and probe operations on the runtime transport."""

    async def inspect_capabilities(self) -> _CapabilitySnapshot:
        """Connect, negotiate MCP, and capture all advertised tool schemas.

        No LogRocket data tool is called. In particular this method does not
        call ``find_sessions`` or ``find_issues``.
        """
        api_key = self._resolve_api_key()
        try:
            import httpx2
            from mcp import Client
            from mcp.client.streamable_http import streamable_http_client

            async with (
                asyncio.timeout(INSPECTION_TIMEOUT),
                httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0
                ) as http_client,
            ):
                transport = streamable_http_client(self.endpoint, http_client=http_client)
                async with Client(transport) as mcp_client:
                    return await _collect_snapshot(mcp_client, self.endpoint)
        except LogRocketMCPError:
            raise
        except Exception as exc:
            # Transport and negotiation exceptions can include headers or other
            # sensitive values. Expose only exception types and suppress chaining.
            raise LogRocketMCPError(
                f"LogRocket MCP capability inspection failed ({_exception_type_names(exc)}); "
                "error details withheld."
            ) from None

    async def probe(
        self, name: str, arguments: dict[str, Any], *, baseline: _CapabilitySnapshot
    ) -> dict[str, Any]:
        """Probe one bounded search after comparing its live definition with a baseline."""
        import asyncio

        validate_request(baseline, name, arguments)
        api_key = self._resolve_api_key()
        try:
            import httpx2
            from mcp import Client
            from mcp.client.streamable_http import streamable_http_client

            async with asyncio.timeout(60):
                async with httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0
                ) as http_client:
                    transport = streamable_http_client(self.endpoint, http_client=http_client)
                    async with Client(transport) as mcp_client:
                        snapshot = await _collect_snapshot(mcp_client, self.endpoint)
                        return await probe_tool(mcp_client, snapshot, baseline, name, arguments)
        except LogRocketMCPError:
            raise
        except Exception:
            # Data-tool failures may contain response content as well as credentials.
            raise LogRocketMCPError(
                "LogRocket response probe failed or timed out; error details withheld."
            ) from None


# Probe
# ----------------------------------------------------------------------------

ALLOWED_TOOLS = frozenset({"find_sessions", "find_issues"})
_ANNOTATION_KEYS = frozenset({"description", "examples", "title"})


def _validation_contract(value: Any) -> Any:
    """Remove JSON Schema annotations that do not affect request validation."""
    if isinstance(value, dict):
        return {
            key: _validation_contract(item)
            for key, item in value.items()
            if key not in _ANNOTATION_KEYS
        }
    if isinstance(value, list):
        return [_validation_contract(item) for item in value]
    return value


def response_shape(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """Keep field names, types, and lengths, never scalar record values.

    Field names can themselves be sensitive in dynamic maps. Review reports
    before sharing them. Depth and array sampling bound the size of reports.
    """
    if depth >= 12:
        return {"type": "depth_limit"}
    if isinstance(value, dict):
        return {
            "type": "object",
            "fields": {key: response_shape(item, depth=depth + 1) for key, item in value.items()},
        }
    if isinstance(value, list):
        shapes = {}
        for item in value[:100]:
            shape = response_shape(item, depth=depth + 1)
            shapes[json.dumps(shape, sort_keys=True)] = shape
        return {
            "type": "array",
            "length": len(value),
            "sampled": min(len(value), 100),
            "item_shapes": list(shapes.values()),
        }
    kind = (
        "null"
        if value is None
        else "boolean"
        if isinstance(value, bool)
        else "string"
        if isinstance(value, str)
        else "number"
    )
    return {"type": kind}


def validate_request(snapshot: _CapabilitySnapshot, name: str, arguments: dict[str, Any]):
    if name not in ALLOWED_TOOLS:
        raise ValueError("Only find_sessions and find_issues may be probed.")
    matches = [tool for tool in snapshot.tools if tool.name == name]
    if len(matches) != 1:
        raise ValueError("The selected tool must be advertised exactly once.")
    tool = matches[0]
    # Require an explicit small budget even if the server advertises a larger default.
    limit = arguments.get("limit")
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Probes require an explicit integer limit between 1 and 5.")
    try:
        validator = validators.validator_for(tool.input_schema)
        validator.check_schema(tool.input_schema)
        validator(tool.input_schema).validate(arguments)
    except (SchemaError, ValidationError):
        # ValidationError text includes the rejected values, which may be sensitive.
        raise ValueError("Probe arguments do not conform to the advertised input schema.") from None
    return tool


async def probe_tool(
    client: Any,
    snapshot: _CapabilitySnapshot,
    baseline: _CapabilitySnapshot,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    tool = validate_request(snapshot, name, arguments)
    previous = [item for item in baseline.tools if item.name == name]
    if len(previous) != 1 or (
        _validation_contract(tool.input_schema) != _validation_contract(previous[0].input_schema)
        or _validation_contract(tool.output_schema)
        != _validation_contract(previous[0].output_schema)
    ):
        raise ValueError("Tool definition changed; inspect and review a fresh baseline first.")
    result = await client.call_tool(name, arguments)
    if result.is_error:
        raise LogRocketMCPError("LogRocket returned a tool error; response content withheld.")
    payload = result.structured_content
    encoding = "structured_content"
    if payload is None:
        blocks = result.content
        if len(blocks) != 1 or getattr(blocks[0], "type", None) != "text":
            raise LogRocketMCPError("Probe response is not a single JSON text or structured value.")
        try:
            payload = json.loads(blocks[0].text)
        except (ValueError, TypeError):
            raise LogRocketMCPError("Probe response text is not JSON; content withheld.") from None
        encoding = "json_text"
    return {
        "tool": name,
        "encoding": encoding,
        "response_shape": response_shape(payload),
        "sync_implementation_allowed": False,
        "note": "Shapes do not prove stable identity, complete pagination, or deletion semantics.",
    }


# Capabilities
# ----------------------------------------------------------------------------

REQUIRED_TOOLS = ("find_sessions", "find_issues")

_CURSOR_INPUT_NAMES = {
    "after",
    "before",
    "cursor",
    "offset",
    "page",
    "page_cursor",
    "page_token",
    "starting_after",
}
_LIMIT_INPUT_NAMES = {"first", "limit", "page_size", "per_page", "size"}
_CONTINUATION_OUTPUT_NAMES = {
    "has_more",
    "next",
    "next_cursor",
    "next_page",
    "next_page_token",
    "next_token",
    "page_info",
}
_IDENTITY_NAMES = {"id", "issue_id", "session_id", "recording_id"}
_DELETION_NAMES = {
    "archived",
    "deleted",
    "deleted_at",
    "deletion",
    "deletions",
    "is_archived",
    "is_deleted",
    "removed",
    "removed_at",
    "tombstone",
    "tombstones",
}
_TIME_NAMES = {
    "created_at",
    "end_time",
    "first_seen",
    "last_seen",
    "start_time",
    "timestamp",
    "updated_at",
}

EvidenceStatus = Literal["schema_evidence", "partial", "unverified", "missing"]


@dataclass(frozen=True)
class ToolCapability:
    name: str
    present: bool
    response_schema: bool
    pagination: EvidenceStatus
    pagination_evidence: tuple[str, ...]
    stable_identity: EvidenceStatus
    identity_evidence: tuple[str, ...]
    timestamps: EvidenceStatus
    timestamp_evidence: tuple[str, ...]
    deletion_visibility: EvidenceStatus
    deletion_evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "present": self.present,
            "response_schema": self.response_schema,
            "pagination": self.pagination,
            "pagination_evidence": list(self.pagination_evidence),
            "stable_identity": self.stable_identity,
            "identity_evidence": list(self.identity_evidence),
            "timestamps": self.timestamps,
            "timestamp_evidence": list(self.timestamp_evidence),
            "deletion_visibility": self.deletion_visibility,
            "deletion_evidence": list(self.deletion_evidence),
        }


@dataclass(frozen=True)
class CapabilityReport:
    endpoint: str
    protocol_version: str | None
    tools: tuple[ToolCapability, ...]
    sync_implementation_allowed: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "protocol_version": self.protocol_version,
            "tools": [tool.to_dict() for tool in self.tools],
            "sync_implementation_allowed": self.sync_implementation_allowed,
            "blockers": list(self.blockers),
        }


def _property_paths(schema: dict[str, Any] | None) -> set[str]:
    """Return dotted paths for every JSON Schema property, including branches."""
    if not schema:
        return set()
    paths: set[str] = set()
    visited: set[int] = set()

    def visit(node: Any, prefix: str = "") -> None:
        if isinstance(node, dict):
            marker = id(node)
            if marker in visited:
                return
            visited.add(marker)
            properties = node.get("properties")
            if isinstance(properties, dict):
                for name, child in properties.items():
                    path = f"{prefix}.{name}" if prefix else str(name)
                    paths.add(path)
                    visit(child, path)
            for key in ("items", "additionalProperties", "contains", "not"):
                visit(node.get(key), prefix)
            for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
                branches = node.get(key)
                if isinstance(branches, list):
                    for branch in branches:
                        visit(branch, prefix)
            definitions = node.get("$defs", node.get("definitions"))
            if isinstance(definitions, dict):
                for child in definitions.values():
                    visit(child, prefix)
        elif isinstance(node, list):
            for child in node:
                visit(child, prefix)

    visit(schema)
    return paths


def _matching_paths(paths: set[str], names: set[str]) -> tuple[str, ...]:
    return tuple(sorted(path for path in paths if path.rsplit(".", 1)[-1].lower() in names))


def _deletion_tools_for(resource_name: str, tools: tuple[_ToolDefinition, ...]) -> tuple[str, ...]:
    resource = resource_name.removeprefix("find_").removesuffix("s")
    evidence: list[str] = []
    for tool in tools:
        lowered = tool.name.lower()
        output_paths = _property_paths(tool.output_schema)
        deletion_paths = _matching_paths(output_paths, _DELETION_NAMES)
        identity_paths = _matching_paths(output_paths, _IDENTITY_NAMES)
        if resource in lowered and deletion_paths and identity_paths:
            evidence.extend(f"tool:{tool.name}:output:{path}" for path in deletion_paths)
    return tuple(sorted(evidence))


def _inspect_tool(name: str, tools: tuple[_ToolDefinition, ...]) -> ToolCapability:
    tool = next((candidate for candidate in tools if candidate.name == name), None)
    if tool is None:
        return ToolCapability(
            name=name,
            present=False,
            response_schema=False,
            pagination="missing",
            pagination_evidence=(),
            stable_identity="missing",
            identity_evidence=(),
            timestamps="missing",
            timestamp_evidence=(),
            deletion_visibility="missing",
            deletion_evidence=(),
        )

    input_paths = _property_paths(tool.input_schema)
    output_paths = _property_paths(tool.output_schema)
    cursor_inputs = _matching_paths(input_paths, _CURSOR_INPUT_NAMES)
    limit_inputs = _matching_paths(input_paths, _LIMIT_INPUT_NAMES)
    continuations = _matching_paths(output_paths, _CONTINUATION_OUTPUT_NAMES)
    pagination_evidence = tuple(
        [*(f"input:{path}" for path in cursor_inputs), *(f"input:{path}" for path in limit_inputs)]
        + [f"output:{path}" for path in continuations]
    )
    if cursor_inputs and continuations:
        pagination: EvidenceStatus = "schema_evidence"
    elif cursor_inputs or limit_inputs or continuations:
        pagination = "partial"
    else:
        pagination = "unverified"

    identities = _matching_paths(output_paths, _IDENTITY_NAMES)
    timestamps = _matching_paths(output_paths, _TIME_NAMES)
    deletion_fields = _matching_paths(output_paths, _DELETION_NAMES)
    deletion_tools = _deletion_tools_for(name, tools)
    deletion_evidence = (
        *(f"output:{path}" for path in deletion_fields),
        *deletion_tools,
    )

    return ToolCapability(
        name=name,
        present=True,
        response_schema=tool.output_schema is not None,
        pagination=pagination,
        pagination_evidence=pagination_evidence,
        stable_identity="schema_evidence" if identities else "unverified",
        identity_evidence=tuple(f"output:{path}" for path in identities),
        timestamps="schema_evidence" if timestamps else "unverified",
        timestamp_evidence=tuple(f"output:{path}" for path in timestamps),
        deletion_visibility="schema_evidence" if deletion_evidence else "unverified",
        deletion_evidence=deletion_evidence,
    )


def inspect_snapshot(snapshot: _CapabilitySnapshot) -> CapabilityReport:
    """Assess whether captured schemas are sufficient to design safe sync."""
    inspected = tuple(_inspect_tool(name, snapshot.tools) for name in REQUIRED_TOOLS)
    blockers: list[str] = []
    for tool in inspected:
        if not tool.present:
            blockers.append(f"{tool.name}: tool is not advertised")
            continue
        if not tool.response_schema:
            blockers.append(f"{tool.name}: no MCP outputSchema is advertised")
        if tool.pagination != "schema_evidence":
            blockers.append(
                f"{tool.name}: exhaustive pagination is not proven by input and output schemas"
            )
        else:
            blockers.append(
                f"{tool.name}: live calls have not proven cursor progress, termination, or limits"
            )
        if tool.stable_identity != "schema_evidence":
            blockers.append(f"{tool.name}: stable record identity is not proven")
        if tool.deletion_visibility != "schema_evidence":
            blockers.append(
                f"{tool.name}: explicit deletion visibility or a deletion/audit tool is not proven"
            )
        else:
            blockers.append(
                f"{tool.name}: live behavior has not proven that deletions are observable"
            )

    return CapabilityReport(
        endpoint=snapshot.endpoint,
        protocol_version=snapshot.protocol_version,
        tools=inspected,
        # tools/list is necessary evidence, not sufficient evidence. Even rich
        # JSON Schemas cannot prove cursor termination or that deleted records
        # are actually returned by the server.
        sync_implementation_allowed=False,
        blockers=tuple(blockers),
    )


# Rest
# ----------------------------------------------------------------------------


class LogRocketRESTError(RuntimeError):
    """A failure that never includes credentials, URLs, cursors or response data."""

    def __init__(self, code: str, *, status: int | None = None):
        self.code = code
        self.status = status
        suffix = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"LogRocket REST {code}{suffix}; response details withheld.")


def _cursor(value: Any) -> str:
    # The docs initially show numeric cursors but explicitly require treating
    # continuations as opaque strings, including same-millisecond tie breakers.
    if isinstance(value, str) and value:
        return value
    if type(value) is int and value >= 0:
        return str(value)
    raise LogRocketRESTError("invalid_cursor")


def _positive_int(value: Any, maximum: int, name: str) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}.")


@dataclass(frozen=True)
class AuditPage:
    records: tuple[dict[str, Any], ...] = field(repr=False)
    has_next: bool
    cursor: str = field(repr=False)


@dataclass(frozen=True)
class AuditBatch:
    """A completely read bounded audit traversal, not a session inventory."""

    records: tuple[dict[str, Any], ...] = field(repr=False)
    cursor: str = field(repr=False)
    pages: int


def parse_audit_page(payload: dict[str, Any], *, limit: int) -> AuditPage:
    if not isinstance(payload, dict):
        raise LogRocketRESTError("invalid_audit_page")
    records = payload.get("logs")
    if not isinstance(records, list) or len(records) > limit:
        raise LogRocketRESTError("invalid_audit_records")
    if type(payload.get("has_next")) is not bool:
        raise LogRocketRESTError("invalid_audit_continuation")
    # Observed live on 2026-10-19: an empty terminal page returns cursor="".
    # This is not a continuation and must never replace the caller's position.
    cursor = (
        ""
        if not records and payload["has_next"] is False and payload.get("cursor") == ""
        else _cursor(payload.get("cursor"))
    )
    for row in records:
        if (
            not isinstance(row, dict)
            or any(
                not isinstance(row.get(key), str)
                for key in ("created_date", "user", "action", "description")
            )
            or ("time" in row and not isinstance(row["time"], str))
        ):
            raise LogRocketRESTError("invalid_audit_record")
    return AuditPage(tuple(records), payload["has_next"], cursor)


class LogRocketRESTClient:
    """Project-scoped GET-only client for documented audit/export endpoints."""

    def __init__(
        self,
        organization_id: str,
        project_id: str,
        *,
        api_key: str | None = None,
        api_key_env: str = "LOGROCKET_API_KEY",
        base_url: str = "https://api.logrocket.com",
        transport: httpx2.AsyncBaseTransport | None = None,
    ):
        if not organization_id.strip() or not project_id.strip():
            raise ValueError("organization_id and project_id are required.")
        if organization_id.strip() in {".", ".."} or project_id.strip() in {".", ".."}:
            raise ValueError("organization_id and project_id cannot be path traversal segments.")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("base_url must be an HTTPS origin without credentials, path or query.")
        self._base = (
            f"{base_url.rstrip('/')}/v1/orgs/{quote(organization_id.strip(), safe='')}"
            f"/apps/{quote(project_id.strip(), safe='')}/"
        )
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._transport = transport

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            api_key = _resolve_api_key(self._api_key, self._api_key_env)
        except _CredentialError as exc:
            raise LogRocketRESTError(exc.code) from None
        try:
            async with asyncio.timeout(60) as deadline:
                async with httpx2.AsyncClient(
                    headers={"Authorization": f"token {api_key}"},
                    timeout=15,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    for attempt in range(3):
                        delay = float(2**attempt)
                        try:
                            response = await client.get(self._base + path, params=params)
                        except (httpx2.TransportError, TimeoutError):
                            if attempt == 2:
                                raise LogRocketRESTError("transport_failure") from None
                        else:
                            status = response.status_code
                            if status in TRANSIENT_STATUS:
                                delay = max(
                                    delay, _retry_after(response.headers.get("Retry-After"))
                                )
                                if attempt == 2:
                                    raise LogRocketRESTError(
                                        "transient_http_failure", status=status
                                    )
                            elif not 200 <= status < 300:
                                raise LogRocketRESTError("http_failure", status=status)
                            else:
                                try:
                                    payload = response.json()
                                except (ValueError, TypeError):
                                    raise LogRocketRESTError(
                                        "invalid_json", status=status
                                    ) from None
                                if not isinstance(payload, dict):
                                    raise LogRocketRESTError("invalid_json_object", status=status)
                                return payload
                        if delay >= deadline.when() - asyncio.get_running_loop().time():
                            raise LogRocketRESTError("retry_deadline_exceeded")
                        await asyncio.sleep(delay)
        except LogRocketRESTError:
            raise
        except TimeoutError:
            raise LogRocketRESTError("timeout") from None
        except Exception:
            raise LogRocketRESTError("request_failure") from None
        raise LogRocketRESTError("request_failure")  # pragma: no cover

    async def audit_page(self, *, cursor: str, limit: int = 100) -> AuditPage:
        _positive_int(limit, 100, "limit")
        payload = await self._get("audit/logs/", {"cursor": _cursor(cursor), "limit": limit})
        return parse_audit_page(payload, limit=limit)

    async def read_audit_logs(
        self,
        *,
        cursor: str,
        limit: int = 100,
        max_pages: int = 100,
    ) -> AuditBatch:
        """Follow has_next to completion or fail, without committing caller state.

        This reads audit activity, including potentially sensitive employee data.
        A batch does not establish deletion IDs, completion, or inventory absence.
        """
        _positive_int(limit, 100, "limit")
        _positive_int(max_pages, 100, "max_pages")
        cursor = _cursor(cursor)
        seen = {cursor}
        records: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(120):
                for page_number in range(1, max_pages + 1):
                    page = await self.audit_page(cursor=cursor, limit=limit)
                    # A terminal page may legitimately echo the last cursor.
                    if page.has_next and (page.cursor in seen or not page.records):
                        raise LogRocketRESTError("audit_cursor_not_progressing")
                    records.extend(page.records)
                    if not page.has_next:
                        return AuditBatch(tuple(records), page.cursor or cursor, page_number)
                    cursor = page.cursor
                    seen.add(cursor)
        except TimeoutError:
            raise LogRocketRESTError("audit_timeout") from None
        raise LogRocketRESTError("audit_page_budget_exceeded")

    async def export_page(
        self,
        *,
        cursor: str | None = None,
        since_ms: int | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Read one legacy export listing; its end-of-stream contract is unverified.

        No export URLs are followed, and this never authorizes replacement or
        cursor advancement in the ingestion source.
        """
        _positive_int(limit, 100, "limit")
        if cursor is not None and since_ms is not None:
            raise ValueError("Use either cursor or since_ms, not both.")
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = _cursor(cursor)
        if since_ms is not None:
            if type(since_ms) is not int or since_ms < 0:
                raise ValueError("since_ms must be a non-negative integer timestamp.")
            params["date"] = since_ms
        payload = await self._get("data-export/", params)
        records = payload.get("sessions")
        if not isinstance(records, list) or len(records) > limit:
            raise LogRocketRESTError("invalid_export_records")
        _cursor(payload.get("cursor"))
        if any(
            not isinstance(row, dict) or not isinstance(row.get("url"), str) or not row["url"]
            for row in records
        ):
            raise LogRocketRESTError("invalid_export_record")
        return payload

    async def probe(
        self,
        resource: str,
        *,
        cursor: str | None = None,
        since_ms: int | None = None,
        limit: int = 2,
        max_pages: int = 1,
    ):
        """Return counts and field types, never record values or opaque cursors."""

        _positive_int(limit, 5, "probe limit")
        _positive_int(max_pages, 5, "probe max_pages")
        if resource not in {"audit", "session-exports"}:
            raise ValueError("resource must be audit or session-exports.")
        if cursor is not None and since_ms is not None:
            raise ValueError("Use either cursor or since_ms, not both.")
        if since_ms is not None and (type(since_ms) is not int or since_ms < 0):
            raise ValueError("since_ms must be a non-negative integer timestamp.")
        if resource == "audit" and cursor is None and since_ms is None:
            raise ValueError("Audit inspection requires cursor or since_ms.")
        report: dict[str, Any] = {
            "resource": resource,
            "automatic_deletion_supported": False,
            "session_inventory_complete": False,
            "issue_inventory_complete": False,
        }
        try:
            if resource == "audit":
                initial = cursor if cursor is not None else str(since_ms)
                batch = await self.read_audit_logs(cursor=initial, limit=limit, max_pages=max_pages)
                known = {"time", "created_date", "user", "action", "description"}
                report.update(
                    {
                        "status": "ok",
                        "pages": batch.pages,
                        "record_count": len(batch.records),
                        "audit_traversal_complete": True,
                        "records_shape": response_shape(
                            [
                                {key: value for key, value in row.items() if key in known}
                                for row in batch.records
                            ]
                        ),
                        "additional_record_fields_present": any(
                            set(row) - known for row in batch.records
                        ),
                        "deletion_contract": "unverified_even_when_audit_traversal_is_complete",
                    }
                )
            else:
                payload = await self.export_page(cursor=cursor, since_ms=since_ms, limit=limit)
                report.update(
                    {
                        "status": "ok",
                        "pages": 1,
                        "record_count": len(payload["sessions"]),
                        "records_shape": response_shape(
                            [{"url": row["url"]} for row in payload["sessions"]]
                        ),
                        "export_completion_contract": "unverified",
                    }
                )
        except LogRocketRESTError as exc:
            report.update({"status": "error", "error_code": exc.code, "http_status": exc.status})
        return report


# Cli
# ----------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cognee-logrocket")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser(
        "inspect-capabilities",
        help="Capture and assess LogRocket MCP tool schemas without calling data tools.",
    )
    source = inspect.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-file",
        type=Path,
        help="Inspect a previously captured JSON snapshot entirely offline.",
    )
    source.add_argument("--organization", help="LogRocket organization ID for live inspection.")
    inspect.add_argument("--project", help="LogRocket project ID for live inspection.")
    inspect.add_argument("--api-key-env", default="LOGROCKET_API_KEY")
    inspect.add_argument(
        "--output",
        type=Path,
        help="Write the complete capability snapshot (including raw schemas) to this file.",
    )
    inspect.add_argument("--json", action="store_true", help="Print the assessment as JSON.")
    inspect.add_argument(
        "--require-sync-ready",
        action="store_true",
        help="Exit 2 unless pagination, stable IDs, and deletion visibility are proven.",
    )
    probe = subparsers.add_parser(
        "probe-response", help="Read at most five search results and print only their structure."
    )
    probe.add_argument("--organization", required=True)
    probe.add_argument("--project", required=True)
    probe.add_argument("--api-key-env", default="LOGROCKET_API_KEY")
    probe.add_argument("--baseline", required=True, type=Path)
    probe.add_argument("--tool", choices=["find_sessions", "find_issues"], required=True)
    probe.add_argument("--arguments-file", required=True, type=Path)
    rest = subparsers.add_parser(
        "probe-rest",
        help="Read documented audit/export pages without modifying LogRocket or Cognee.",
    )
    rest.add_argument("--organization", required=True)
    rest.add_argument("--project", required=True)
    rest.add_argument("--api-key-env", default="LOGROCKET_API_KEY")
    rest.add_argument("--resource", choices=["audit", "session-exports"], required=True)
    position = rest.add_mutually_exclusive_group()
    position.add_argument("--cursor", help="Opaque cursor returned by the previous page.")
    position.add_argument("--since-ms", type=int, help="Initial Unix timestamp in milliseconds.")
    rest.add_argument("--limit", type=int, default=2)
    rest.add_argument("--max-pages", type=int, default=1)
    return parser


def _read_snapshot(path: Path) -> _CapabilitySnapshot:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Capability snapshot root must be a JSON object.")
    return _CapabilitySnapshot.from_dict(value)


def _write_snapshot(path: Path, snapshot: _CapabilitySnapshot) -> None:
    path.write_text(
        json.dumps(snapshot.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _format_report(report) -> str:
    lines = [
        f"Endpoint: {report.endpoint}",
        f"Protocol: {report.protocol_version or 'not reported'}",
    ]
    for tool in report.tools:
        lines.append(
            f"{tool.name}: present={tool.present}, output_schema={tool.response_schema}, "
            f"pagination={tool.pagination}, stable_identity={tool.stable_identity}, "
            f"deletion_visibility={tool.deletion_visibility}"
        )
    verdict = "YES" if report.sync_implementation_allowed else "NO"
    lines.append(f"Safe to implement sync from this evidence: {verdict}")
    if report.blockers:
        lines.append("Blockers:")
        lines.extend(f"- {blocker}" for blocker in report.blockers)
    return "\n".join(lines)


async def _live_snapshot(args: argparse.Namespace) -> _CapabilitySnapshot:
    if not args.project:
        raise ValueError("--project is required with --organization.")
    client = _LogRocketMCPClient(
        organization_id=args.organization,
        project_id=args.project,
        api_key_env=args.api_key_env,
    )
    return await client.inspect_capabilities()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "probe-rest":
            client = LogRocketRESTClient(
                args.organization, args.project, api_key_env=args.api_key_env
            )
            result = asyncio.run(
                client.probe(
                    args.resource,
                    cursor=args.cursor,
                    since_ms=args.since_ms,
                    limit=args.limit,
                    max_pages=args.max_pages,
                )
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["status"] == "ok" else 1
        if args.command == "probe-response":
            arguments = json.loads(args.arguments_file.read_text(encoding="utf-8"))
            if not isinstance(arguments, dict):
                raise ValueError("Probe arguments must be a JSON object.")
            client = _LogRocketMCPClient(
                args.organization, args.project, api_key_env=args.api_key_env
            )
            result = asyncio.run(
                client.probe(args.tool, arguments, baseline=_read_snapshot(args.baseline))
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        snapshot = (
            _read_snapshot(args.from_file)
            if args.from_file is not None
            else asyncio.run(_live_snapshot(args))
        )
        if args.output is not None:
            _write_snapshot(args.output, snapshot)
        report = inspect_snapshot(snapshot)
        print(
            json.dumps(report.to_dict(), indent=2, sort_keys=True)
            if args.json
            else _format_report(report)
        )
        return 2 if args.require_sync_ready and not report.sync_implementation_allowed else 0
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"Capability inspection failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
