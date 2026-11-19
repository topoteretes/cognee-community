"""DLT source for LogRocket session metadata and issue documents.

Search results are merged into retained staging rows. Search absence is never
treated as deletion, and no incremental watermark assumes exhaustive results.
Live MCP input schemas are validated before querying; record parsers validate
the response fields used to build deterministic, redacted documents.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import MethodType
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

import httpx2
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from jsonschema import validators
from jsonschema.exceptions import SchemaError, ValidationError

logger = get_logger("logrocket_connector")


class _CallbackError(Exception):
    """Keep host cancellation failures out of transport retry/redaction handlers."""


def _invoke_callback(callback):
    if callback is not None:
        try:
            callback()
        except Exception as exc:
            raise _CallbackError from exc


# Authentication
# ----------------------------------------------------------------------------


class _CredentialError(RuntimeError):
    """Only a fixed error code may leave credential-provider resolution."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"LogRocket credential resolution failed: {code}; details withheld.")


def _resolve_api_key(api_key: str | None, api_key_env: str = "LOGROCKET_API_KEY") -> str:
    """Resolve explicit value, selected environment variable, then DLT secrets.

    DLT's ``sources.logrocket.api_key`` key supports its normal providers, including
    ``SOURCES__LOGROCKET__API_KEY`` and ``.dlt/secrets.toml``. Do not cache secrets,
    log provider errors, or fall through after an invalid higher-priority value.
    """
    value = api_key
    if value is None:
        value = os.environ.get(api_key_env) or None
    if value is None:
        try:
            import dlt

            value = dlt.secrets.get("sources.logrocket.api_key")
        except Exception:
            raise _CredentialError("credential_provider_failure") from None
    if value is None:
        raise _CredentialError("missing_api_key")
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise _CredentialError("invalid_api_key")
    return value


# Transport retries
# ----------------------------------------------------------------------------

TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


def _leaves(error: BaseException):
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            yield from _leaves(child)
    else:
        yield error


def _retryable(error: BaseException) -> bool:
    return all(
        (isinstance(item, httpx2.HTTPStatusError) and item.response.status_code in TRANSIENT_STATUS)
        or isinstance(item, (httpx2.TransportError, TimeoutError))
        for item in _leaves(error)
    )


def _retry_after(value: str | None, *, now: datetime | None = None) -> float:
    if not value:
        return 0
    if value.isascii() and value.isdigit():
        # Huge valid delays should fail the caller's deadline, not overflow.
        return float(value) if len(value) < 15 else float("inf")
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return 0
        return max(0, (date - (now or datetime.now(UTC))).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return 0


def _retry_delay(error: BaseException, attempt: int) -> float:
    return max(
        [float(2**attempt)]
        + [
            _retry_after(item.response.headers.get("Retry-After"))
            for item in _leaves(error)
            if isinstance(item, httpx2.HTTPStatusError)
        ]
    )


# Content redaction
# ----------------------------------------------------------------------------

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
_IPV4 = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_LOGROCKET_KEY = re.compile(r"\bpat:[^\s:'\"]+:[^\s:'\"]+:[A-Za-z0-9_-]+\b")


def _redact_text(value: object, *, limit: int = 20_000) -> str:
    """Return bounded text with common credentials and direct identifiers removed."""
    if not isinstance(value, str):
        return ""
    text = value[:limit]
    text = _LOGROCKET_KEY.sub("[redacted-logrocket-key]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _EMAIL.sub("[redacted-email]", text)
    return _IPV4.sub("[redacted-ip]", text)


# MCP transport and runtime schema validation
# ----------------------------------------------------------------------------

DEFAULT_MCP_URL = "https://mcp.logrocket.com/mcp"
DEFAULT_API_KEY_ENV = "LOGROCKET_API_KEY"
DEFAULT_TOOLSETS = ("sessions", "issues")


def _reject_json_constant(value: str):
    raise ValueError("Non-finite JSON number.")


class LogRocketMCPError(RuntimeError):
    """A secret-safe LogRocket MCP connection or protocol error."""


@dataclass(frozen=True)
class _ToolDefinition:
    """Advertised tool schemas used to validate requests before data access."""

    name: str
    description: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None


def _project_endpoint(
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


def _exception_type_names(exc: BaseException) -> str:
    """Return exception class names only, including nested exception groups."""
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, tuple):
        names = sorted({_exception_type_names(item) for item in nested})
        return ",".join(names)
    return type(exc).__name__


async def _collect_tools(mcp_client: Any, check_active=None) -> tuple[_ToolDefinition, ...]:
    """Collect every tools/list page from an already-connected MCP client."""
    tools: list[_ToolDefinition] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_tools: set[str] = set()

    while True:
        if len(seen_cursors) >= 100:
            raise LogRocketMCPError("tools/list exceeded the 100-page inspection budget.")
        _invoke_callback(check_active)
        page = await mcp_client.list_tools(cursor=cursor, cache_mode="bypass")
        for tool in page.tools:
            if tool.name in seen_tools:
                raise LogRocketMCPError("tools/list returned duplicate tool definitions.")
            seen_tools.add(tool.name)
            tools.append(
                _ToolDefinition(
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

    return tuple(tools)


class _LogRocketMCPClient:
    """Read-only wrapper around the official MCP SDK's Streamable HTTP client."""

    def __init__(
        self,
        organization_id: str,
        project_id: str,
        *,
        api_key: str | None = None,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        base_url: str = DEFAULT_MCP_URL,
        check_active: Callable[[], None] | None = None,
    ) -> None:
        self.endpoint = _project_endpoint(organization_id, project_id, base_url=base_url)
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._check_active = check_active

    def _resolve_api_key(self) -> str:
        try:
            return _resolve_api_key(self._api_key, self._api_key_env)
        except _CredentialError as exc:
            raise LogRocketMCPError(
                f"LogRocket API key unavailable ({exc.code}). Pass api_key, set "
                "LOGROCKET_API_KEY (or the selected --api-key-env), or configure "
                "sources.logrocket.api_key with DLT secrets; details withheld."
            ) from None

    async def search(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one bounded session or issue search and return its JSON object.

        Output validation belongs to the resource-specific parser because the
        live tools advertise no MCP output schema.
        """
        import asyncio

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
                    tools = await _collect_tools(mcp_client, self._check_active)
                    matches = [tool for tool in tools if tool.name == name]
                    if len(matches) != 1:
                        raise LogRocketMCPError(
                            "Required LogRocket search tool is missing or duplicated."
                        )
                    schema = matches[0].input_schema
                    validator = validators.validator_for(schema)
                    validator.check_schema(schema)
                    validator(schema).validate(arguments)
                    _invoke_callback(self._check_active)
                    result = await mcp_client.call_tool(name, arguments)
                    _invoke_callback(self._check_active)
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
                            payload = json.loads(
                                blocks[0].text, parse_constant=_reject_json_constant
                            )
                        except ValueError:
                            raise LogRocketMCPError(
                                "LogRocket search response contains malformed JSON; "
                                "response content withheld."
                            ) from None
                    if not isinstance(payload, dict):
                        raise LogRocketMCPError("LogRocket search response must be an object.")
                    return payload

        last_error: BaseException | None = None
        try:
            async with asyncio.timeout(100) as deadline:
                for attempt in range(3):
                    _invoke_callback(self._check_active)
                    try:
                        return await search_once()
                    except _CallbackError:
                        raise
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
                    except Exception as exc:
                        last_error = exc
                        if not _retryable(exc):
                            raise LogRocketMCPError(
                                f"LogRocket search failed ({_exception_type_names(exc)}); "
                                "error details withheld."
                            ) from None
                        if attempt < 2:
                            delay = _retry_delay(exc, attempt)
                            if delay >= deadline.when() - asyncio.get_running_loop().time():
                                raise LogRocketMCPError(
                                    "LogRocket retry delay exceeds the search deadline."
                                ) from None
                            await asyncio.sleep(delay)
                raise LogRocketMCPError(
                    f"LogRocket search failed ({_exception_type_names(last_error)}); "
                    "error details withheld."
                )
        except _CallbackError as exc:
            raise exc.__cause__ from None
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


# Document rendering and ingestion
# ----------------------------------------------------------------------------

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
            or _redact_text(unquote(value)) != unquote(value)
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
        unsafe |= any(_redact_text(component) != component for component in decoded_components)
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
    return _redact_text(value)


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


def _session_to_row(
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
    if _redact_text(recording_id) != recording_id:
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


def _issue_to_row(record: dict[str, Any], organization_id: str, project_id: str) -> dict[str, str]:
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
        f"Issue type: {_redact_text(issue_type)}",
        f"First observed: {_redact_text(earliest)}",
        f"Last observed: {_redact_text(latest)}",
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
        "title": title or f"LogRocket {_redact_text(issue_type)} issue",
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
    stats: dict[str, int] | None = None,
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

    if stats is not None:
        stats["requests"] += 1
    response = _run(client.search(f"find_{resource}", arguments))
    if not isinstance(response, dict) or not isinstance(response.get(container), list):
        raise ValueError(f"LogRocket find_{resource} response has an unexpected structure.")
    records = response[container]
    if stats is not None:
        stats["scanned"] = len(records)
    if len(records) >= limit:
        raise IncompleteLogRocketResultError(
            f"find_{resource} returned the configured limit ({limit}); result may be truncated. "
            "Narrow the query or time window before retrying."
        )

    parser = _session_to_row if resource == "sessions" else _issue_to_row
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
    if stats is not None:
        stats["skipped"] = len(rows) - len(unique)
    yield from unique.values()
    logger.info("LogRocket: emitted %d %s document(s) for merge.", len(unique), resource)


# Public source factory
# ----------------------------------------------------------------------------


def logrocket_source(
    organization_id: str,
    project_id: str,
    *,
    resources: Iterable[str] = ("sessions", "issues"),
    api_key: str | None = None,
    client: Any = None,
    check_active: Callable[[], None] | None = None,
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
    """Create a DLT source for LogRocket session and issue documents.

    Each run requests the full configured window, without a cursor or hash cache.
    Explicit timezone-aware boundaries allow repeatable historical imports.
    Issue dates filter occurrences, not modification times; re-fetching the
    retained window makes old issues eligible for updated triage and content.
    Search absence is never emitted as a tombstone. Pass this source to
    ``remember_logrocket`` or use ``cognee.remember(..., write_disposition="merge")``.

    Args:
        organization_id: LogRocket organization identifier.
        project_id: Project identifier within the organization.
        resources: Select sessions, issues, or both.
        api_key: API key; falls back to LOGROCKET_API_KEY, then DLT secrets.
        client: Injected async search client, primarily for offline tests.
        check_active: Synchronous host authorization/cancellation callback. Checked
            before extraction, each built-in transport attempt, and publishing rows.
        session_query: Additional natural-language session search criteria.
        issue_query: Additional natural-language issue search criteria.
        session_lookback_days: Session occurrence window when start is omitted.
        issue_lookback_days: Issue occurrence window when start is omitted.
        window_start: Explicit timezone-aware start for both resource windows.
        window_end: Explicit timezone-aware end; defaults to the current time.
        limit: Maximum requested results per resource, between 1 and 100.
        include_user_id: Include user identifiers in session documents (opt-in).
        include_location: Include location in session documents (opt-in).
        acknowledge_bounded_results: Accept that search is not exhaustive and
            cannot discover deletion; must be True before extraction.

    Returns:
        A document-tagged DLT source with stable primary keys and merge writes.
        Use remember_logrocket to preserve merge semantics at the Cognee boundary.
    """
    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError("LogRocket requires Cognee table-scoped document sync support.")
    if check_active is not None and not callable(check_active):
        raise ValueError("check_active must be callable.")
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

    search_client = client or _LogRocketMCPClient(
        organization_id, project_id, api_key=api_key, check_active=check_active
    )
    stats = {name: {} for name in selected}

    def extract(name, **options):
        counters = stats[name]
        counters.clear()
        counters.update(
            dict.fromkeys(
                (
                    "scanned",
                    "emitted",
                    "skipped",
                    "deleted",
                    "failed",
                    "requests",
                    "request_complete",
                ),
                0,
            )
        )
        try:
            if check_active is not None:
                check_active()
            require_merge(f"logrocket_{name}")
            rows = _fetch_rows(search_client, name, stats=counters, **options)
            for row in dlt_utils.guarded_rows(rows, check_active):
                counters["emitted"] += 1
                yield row
            counters["request_complete"] = 1
        except BaseException:
            counters["failed"] = 1
            raise

    def require_merge(resource_name):
        if dlt.current.source().resources[resource_name].write_disposition != "merge":
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
        yield from extract(
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
        yield from extract(
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
        dlt_utils.PIPELINE_SCOPE_ATTR: "logrocket_"
        + hashlib.sha256(json.dumps([organization_id, project_id]).encode()).hexdigest()[:32],
        "cognee_sync_stats": stats,
    }

    def attach_metadata(source: Any) -> None:
        # DLT source cloning reconstructs the source without custom attributes.
        # Preserve Cognee's shared scope marker and host counters on with_resources().
        original_clone = source.clone

        def clone_with_metadata(source: Any, with_name: str | None = None):
            cloned = original_clone(with_name=with_name)
            attach_metadata(cloned)
            return cloned

        for key, value in metadata.items():
            setattr(source, key, value)
        for name in selected:
            source.resources[f"logrocket_{name}"].cognee_sync_stats = stats[name]
        source.clone = MethodType(clone_with_metadata, source)

    attach_metadata(result)
    return result


async def remember_logrocket(source: Any, *, dataset_name: str | None = None, **kwargs: Any):
    """Ingest into a dedicated dataset with merge enforced at the Cognee boundary.

    Cognee otherwise overrides resource hints with its default ``replace``.
    Re-read retained staging rows so absent search results cannot trigger cleanup.

    Args:
        source: Source returned by logrocket_source, optionally resource-selected.
        dataset_name: Dedicated dataset; defaults to a project/resource-scoped name.
        **kwargs: Additional cognee.remember options. Conflicting write disposition
            or primary key overrides are rejected before ingestion.

    Returns:
        The result of cognee.remember.
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
