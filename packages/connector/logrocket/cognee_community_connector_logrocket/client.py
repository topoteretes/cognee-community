"""Small, read-only client for inspecting LogRocket's MCP capabilities.

Discovery captures live MCP tool definitions. Explicit bounded probes can
also inspect search response structure without assuming record fields.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from cognee_community_connector_logrocket.credentials import CredentialError, resolve_api_key

DEFAULT_MCP_URL = "https://mcp.logrocket.com/mcp"
DEFAULT_API_KEY_ENV = "LOGROCKET_API_KEY"
DEFAULT_TOOLSETS = ("sessions", "issues")


class LogRocketMCPError(RuntimeError):
    """A secret-safe LogRocket MCP connection or protocol error."""


@dataclass(frozen=True)
class ToolDefinition:
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
    def from_dict(cls, value: dict[str, Any]) -> ToolDefinition:
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
class CapabilitySnapshot:
    """Lossless-enough capture of MCP discovery and all ``tools/list`` pages."""

    endpoint: str
    protocol_version: str | None
    server_info: dict[str, Any] | None
    server_capabilities: dict[str, Any]
    tools: tuple[ToolDefinition, ...]

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
    def from_dict(cls, value: dict[str, Any]) -> CapabilitySnapshot:
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
            tools=tuple(ToolDefinition.from_dict(tool) for tool in tools),
        )


def project_endpoint(
    organization_id: str,
    project_id: str,
    *,
    base_url: str = DEFAULT_MCP_URL,
    toolsets: tuple[str, ...] = DEFAULT_TOOLSETS,
) -> str:
    """Build a least-privilege, project-scoped LogRocket MCP endpoint."""
    if not organization_id.strip() or not project_id.strip():
        raise ValueError("organization_id and project_id are required.")
    if organization_id.strip() in {".", ".."} or project_id.strip() in {".", ".."}:
        raise ValueError("organization_id and project_id cannot be path traversal segments.")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("base_url must be an absolute HTTPS URL without credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query string or fragment.")
    path = (
        f"{base_url.rstrip('/')}/{quote(organization_id.strip(), safe='')}"
        f"/{quote(project_id.strip(), safe='')}"
    )
    return f"{path}?{urlencode({'toolsets': ','.join(toolsets)})}"


def _model_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", by_alias=True, exclude_none=True)
    return {"value": str(value)}


def _exception_type_names(exc: BaseException) -> str:
    """Return exception class names only, including nested exception groups."""
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, tuple):
        names = sorted({_exception_type_names(item) for item in nested})
        return ",".join(names)
    return type(exc).__name__


async def _collect_snapshot(mcp_client: Any, endpoint: str) -> CapabilitySnapshot:
    """Collect every tools/list page from an already-connected MCP client."""
    tools: list[ToolDefinition] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_tools: set[str] = set()

    while True:
        if len(seen_cursors) >= 100:
            raise LogRocketMCPError("tools/list exceeded the 100-page inspection budget.")
        page = await mcp_client.list_tools(cursor=cursor, cache_mode="bypass")
        for tool in page.tools:
            if tool.name in seen_tools:
                raise LogRocketMCPError("tools/list returned duplicate tool definitions.")
            seen_tools.add(tool.name)
            tools.append(
                ToolDefinition(
                    name=tool.name,
                    description=getattr(tool, "description", None),
                    input_schema=dict(tool.input_schema),
                    output_schema=(
                        dict(tool.output_schema)
                        if getattr(tool, "output_schema", None) is not None
                        else None
                    ),
                )
            )

        next_cursor = getattr(page, "next_cursor", None)
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str) or not next_cursor:
            raise LogRocketMCPError("tools/list returned an invalid continuation cursor.")
        if next_cursor in seen_cursors:
            raise LogRocketMCPError(
                "tools/list repeated a continuation cursor; inspection aborted."
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    server_info_value = getattr(mcp_client, "server_info", None)
    return CapabilitySnapshot(
        endpoint=endpoint,
        protocol_version=getattr(mcp_client, "protocol_version", None),
        server_info=_model_dict(server_info_value) if server_info_value is not None else None,
        server_capabilities=_model_dict(getattr(mcp_client, "server_capabilities", None)),
        tools=tuple(tools),
    )


class LogRocketMCPClient:
    """Read-only wrapper around the official MCP SDK's Streamable HTTP client."""

    def __init__(
        self,
        organization_id: str,
        project_id: str,
        *,
        api_key: str | None = None,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        base_url: str = DEFAULT_MCP_URL,
    ) -> None:
        self.endpoint = project_endpoint(organization_id, project_id, base_url=base_url)
        self._api_key = api_key
        self._api_key_env = api_key_env

    def _resolve_api_key(self) -> str:
        try:
            return resolve_api_key(self._api_key, self._api_key_env)
        except CredentialError as exc:
            raise LogRocketMCPError(
                f"LogRocket API key unavailable ({exc.code}). Pass api_key, set "
                "LOGROCKET_API_KEY (or the selected --api-key-env), or configure "
                "sources.logrocket.api_key with DLT secrets; details withheld."
            ) from None

    async def inspect_capabilities(self) -> CapabilitySnapshot:
        """Connect, negotiate MCP, and capture all advertised tool schemas.

        No LogRocket data tool is called. In particular this method does not
        call ``find_sessions`` or ``find_issues``.
        """
        api_key = self._resolve_api_key()
        try:
            import httpx2
            from mcp import Client
            from mcp.client.streamable_http import streamable_http_client

            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0
            ) as http_client:
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
        self, name: str, arguments: dict[str, Any], *, baseline: CapabilitySnapshot
    ) -> dict[str, Any]:
        """Probe one bounded search after comparing its live definition with a baseline."""
        import asyncio

        from cognee_community_connector_logrocket.probe import probe_tool, validate_request

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

    async def search(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one bounded session or issue search and return its JSON object.

        Output validation belongs to the resource-specific parser because the
        live tools advertise no MCP output schema.
        """
        import asyncio

        from jsonschema import validators
        from jsonschema.exceptions import SchemaError, ValidationError

        if name not in {"find_sessions", "find_issues"}:
            raise ValueError("Only find_sessions and find_issues are supported.")
        limit = arguments.get("limit")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Searches require an explicit integer limit between 1 and 100.")
        api_key = self._resolve_api_key()
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        async def search_once():
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0
            ) as http_client:
                transport = streamable_http_client(self.endpoint, http_client=http_client)
                async with Client(transport) as mcp_client:
                    snapshot = await _collect_snapshot(mcp_client, self.endpoint)
                    matches = [tool for tool in snapshot.tools if tool.name == name]
                    if len(matches) != 1:
                        raise LogRocketMCPError(
                            "Required LogRocket search tool is missing or duplicated."
                        )
                    schema = matches[0].input_schema
                    validator = validators.validator_for(schema)
                    validator.check_schema(schema)
                    validator(schema).validate(arguments)
                    result = await mcp_client.call_tool(name, arguments)
                    if result.is_error:
                        raise LogRocketMCPError(
                            "LogRocket returned a search error; response content withheld."
                        )
                    payload = result.structured_content
                    if payload is None:
                        blocks = result.content
                        if len(blocks) != 1 or getattr(blocks[0], "type", None) != "text":
                            raise LogRocketMCPError(
                                "LogRocket search response is not one JSON value."
                            )
                        try:
                            payload = json.loads(blocks[0].text)
                        except json.JSONDecodeError:
                            raise LogRocketMCPError(
                                "LogRocket search response contains malformed JSON; "
                                "response content withheld."
                            ) from None
                    if not isinstance(payload, dict):
                        raise LogRocketMCPError("LogRocket search response must be an object.")
                    return payload

        last_error: BaseException | None = None
        try:
            async with asyncio.timeout(100):
                for attempt in range(3):
                    try:
                        return await search_once()
                    except LogRocketMCPError:
                        raise
                    except ValueError:
                        raise LogRocketMCPError(
                            "LogRocket search failed (ValueError); error details withheld."
                        ) from None
                    except (SchemaError, ValidationError):
                        raise LogRocketMCPError(
                            "Search arguments no longer conform to the live LogRocket schema."
                        ) from None
                    except httpx2.HTTPStatusError as exc:
                        status = exc.response.status_code
                        if status not in {408, 429, 500, 502, 503, 504}:
                            raise LogRocketMCPError(
                                f"LogRocket HTTP request failed with status {status}; "
                                "error details withheld."
                            ) from None
                        last_error = exc
                        if attempt < 2:
                            await asyncio.sleep(2**attempt)
                    except Exception as exc:
                        last_error = exc
                        if attempt < 2:
                            await asyncio.sleep(2**attempt)
                raise LogRocketMCPError(
                    f"LogRocket search failed ({_exception_type_names(last_error)}); "
                    "error details withheld."
                )
        except LogRocketMCPError:
            raise
        except TimeoutError:
            raise LogRocketMCPError("LogRocket search timed out after bounded retries.") from None
        except Exception as exc:
            last_error = last_error or exc
            raise LogRocketMCPError(
                f"LogRocket search failed ({_exception_type_names(last_error)}); "
                "error details withheld."
            ) from None
