from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognee_community_connector_logrocket.client import (
    LogRocketMCPClient,
    LogRocketMCPError,
    _collect_snapshot,
    project_endpoint,
)


class FakeMCPClient:
    protocol_version = "test-protocol"

    def __init__(self, pages):
        self.pages = pages
        self.seen_cursors = []
        self.server_info = {"name": "fake-logrocket", "version": "1"}
        self.server_capabilities = {"tools": {"listChanged": False}}

    async def list_tools(self, *, cursor=None, cache_mode=None):
        self.seen_cursors.append((cursor, cache_mode))
        return self.pages[cursor]


def tool(name, *, input_schema=None, output_schema=None):
    return SimpleNamespace(
        name=name,
        description=f"{name} description",
        input_schema=input_schema or {"type": "object", "properties": {}},
        output_schema=output_schema,
    )


def test_project_endpoint_is_project_scoped_and_limits_toolsets():
    endpoint = project_endpoint("my org", "web/app")

    assert endpoint == (
        "https://mcp.logrocket.com/mcp/my%20org/web%2Fapp?toolsets=sessions%2Cissues"
    )


def test_project_endpoint_rejects_ambiguous_base_url():
    with pytest.raises(ValueError, match="query string"):
        project_endpoint("org", "project", base_url="https://example.test/mcp?x=1")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test/mcp",
        "https://synthetic-user:synthetic-key@example.test/mcp",
    ],
)
def test_project_endpoint_requires_secret_safe_https(url):
    with pytest.raises(ValueError, match="HTTPS URL without credentials"):
        project_endpoint("org", "project", base_url=url)


@pytest.mark.parametrize("organization,project", [(".", "app"), ("org", "..")])
def test_project_endpoint_rejects_scope_traversal(organization, project):
    with pytest.raises(ValueError, match="path traversal"):
        project_endpoint(organization, project)


def test_missing_api_key_fails_before_network(monkeypatch):
    import dlt

    monkeypatch.delenv("LOGROCKET_API_KEY", raising=False)
    monkeypatch.setattr(dlt, "secrets", SimpleNamespace(get=lambda _: None))
    client = LogRocketMCPClient("org", "project")

    with pytest.raises(LogRocketMCPError, match="LOGROCKET_API_KEY"):
        client._resolve_api_key()


@pytest.mark.parametrize("stage", ["http_client", "discovery"])
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, ExceptionGroup])
def test_inspection_withholds_credentials_and_error_details(monkeypatch, stage, error_type):
    import asyncio
    import traceback

    import httpx2
    import mcp
    import mcp.client.streamable_http as transport_module

    secret = "synthetic-inspection-secret"
    detail = f"Invalid header Bearer {secret}; private-response-data"
    error = (
        ExceptionGroup(detail, [ValueError(detail)])
        if error_type is ExceptionGroup
        else error_type(detail)
    )

    @asynccontextmanager
    async def http_client(**kwargs):
        if stage == "http_client":
            raise error
        yield object()

    @asynccontextmanager
    async def connect(transport):
        yield SimpleNamespace(list_tools=AsyncMock(side_effect=error))

    monkeypatch.setattr(httpx2, "AsyncClient", http_client)
    monkeypatch.setattr(
        transport_module, "streamable_http_client", lambda *args, **kwargs: "offline"
    )
    monkeypatch.setattr(mcp, "Client", connect)
    client = LogRocketMCPClient("org", "project", api_key=secret)
    with pytest.raises(LogRocketMCPError, match="inspection failed") as caught:
        asyncio.run(client.inspect_capabilities())

    rendered = "".join(traceback.format_exception(caught.value))
    assert secret not in rendered
    assert "private-response-data" not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_collect_snapshot_follows_all_tools_list_pages_without_changing_schemas():
    first_schema = {"type": "object", "properties": {"cursor": {"type": "string"}}}
    second_schema = {
        "type": "object",
        "properties": {"results": {"type": "array", "items": {"type": "object"}}},
    }
    fake = FakeMCPClient(
        {
            None: SimpleNamespace(
                tools=[tool("find_sessions", input_schema=first_schema)], next_cursor="page-2"
            ),
            "page-2": SimpleNamespace(
                tools=[tool("find_issues", output_schema=second_schema)], next_cursor=None
            ),
        }
    )

    import asyncio

    snapshot = asyncio.run(_collect_snapshot(fake, "https://example.test/mcp"))

    assert fake.seen_cursors == [(None, "bypass"), ("page-2", "bypass")]
    assert [item.name for item in snapshot.tools] == ["find_sessions", "find_issues"]
    assert snapshot.tools[0].input_schema == first_schema
    assert snapshot.tools[1].output_schema == second_schema


def test_collect_snapshot_rejects_repeated_tools_list_cursor():
    fake = FakeMCPClient(
        {
            None: SimpleNamespace(tools=[], next_cursor="again"),
            "again": SimpleNamespace(tools=[], next_cursor="again"),
        }
    )

    import asyncio

    with pytest.raises(LogRocketMCPError, match="repeated"):
        asyncio.run(_collect_snapshot(fake, "https://example.test/mcp"))


def test_collect_snapshot_matches_official_mcp_client_api():
    """Exercise the SDK boundary in-process, without HTTP or LogRocket credentials."""
    import asyncio

    from mcp import Client
    from mcp.server import MCPServer

    server = MCPServer("offline-logrocket-fixture")

    @server.tool()
    def find_sessions(cursor: str | None = None) -> dict[str, list[str]]:
        """Synthetic tool used only to verify SDK compatibility."""
        del cursor
        return {"session_ids": []}

    async def inspect():
        async with Client(server) as official_client:
            return await _collect_snapshot(official_client, "in-process")

    snapshot = asyncio.run(inspect())

    assert snapshot.protocol_version
    assert [item.name for item in snapshot.tools] == ["find_sessions"]
    assert "cursor" in snapshot.tools[0].input_schema["properties"]
    assert snapshot.tools[0].output_schema is not None


def test_collect_snapshot_rejects_duplicate_definitions():
    import asyncio

    fake = FakeMCPClient(
        {
            None: SimpleNamespace(
                tools=[tool("find_sessions"), tool("find_sessions")], next_cursor=None
            )
        }
    )
    with pytest.raises(LogRocketMCPError, match="duplicate"):
        asyncio.run(_collect_snapshot(fake, "in-process"))


def test_collect_snapshot_bounds_never_ending_catalog():
    import asyncio

    pages = {None: SimpleNamespace(tools=[], next_cursor="0")}
    pages.update({str(i): SimpleNamespace(tools=[], next_cursor=str(i + 1)) for i in range(101)})
    fake = FakeMCPClient(pages)
    with pytest.raises(LogRocketMCPError, match="budget"):
        asyncio.run(_collect_snapshot(fake, "in-process"))
    assert len(fake.seen_cursors) == 100


@pytest.fixture
def search_transport(monkeypatch):
    """Exercise search(), replacing only its HTTP/MCP transport boundary."""
    import asyncio

    import httpx2
    import mcp
    import mcp.client.streamable_http as transport_module

    attempts = []
    calls = []
    outcomes = []
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "maximum": 100}},
        "required": ["query"],
        "additionalProperties": False,
    }

    @asynccontextmanager
    async def http_client(**kwargs):
        attempts.append(kwargs)
        yield object()

    class Session(FakeMCPClient):
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    @asynccontextmanager
    async def connect(transport):
        assert transport == "offline-transport"
        yield Session(
            {
                None: SimpleNamespace(
                    tools=[tool("find_sessions", input_schema=schema)], next_cursor=None
                )
            }
        )

    monkeypatch.setattr(httpx2, "AsyncClient", http_client)
    monkeypatch.setattr(
        transport_module, "streamable_http_client", lambda *args, **kwargs: "offline-transport"
    )
    monkeypatch.setattr(mcp, "Client", connect)
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return SimpleNamespace(
        client=LogRocketMCPClient("org", "project", api_key="fixture-secret"),
        attempts=attempts,
        calls=calls,
        outcomes=outcomes,
        schema=schema,
        sleep=sleep,
    )


def result(payload=None, *, text=None, error=False):
    return SimpleNamespace(
        is_error=error,
        structured_content=payload,
        content=[] if text is None else [SimpleNamespace(type="text", text=text)],
    )


@pytest.mark.parametrize(
    "response", [result({"sessionDefinitions": []}), result(text='{"sessionDefinitions": []}')]
)
def test_search_accepts_structured_and_text_json(search_transport, response):
    import asyncio

    fake = search_transport
    fake.outcomes.append(response)
    assert asyncio.run(fake.client.search("find_sessions", {"query": "test", "limit": 5})) == {
        "sessionDefinitions": []
    }
    assert fake.attempts[0]["headers"] == {"Authorization": "Bearer fixture-secret"}


def test_search_validates_against_live_schema_before_call(search_transport):
    import asyncio

    fake = search_transport
    fake.schema["properties"]["limit"]["maximum"] = 2
    with pytest.raises(LogRocketMCPError, match="live LogRocket schema"):
        asyncio.run(fake.client.search("find_sessions", {"query": "test", "limit": 5}))
    assert fake.calls == []
    assert len(fake.attempts) == 1


@pytest.mark.parametrize(
    "response",
    [
        result(text="fixture-secret malformed JSON"),
        result([], error=False),
        result(error=True),
        result(text="[]"),
        result(),
        ValueError("fixture-secret"),
    ],
)
def test_search_rejects_permanent_response_errors_without_retry(search_transport, response):
    import asyncio

    fake = search_transport
    fake.outcomes.append(response)
    with pytest.raises(LogRocketMCPError) as caught:
        asyncio.run(fake.client.search("find_sessions", {"query": "test", "limit": 5}))
    assert "fixture-secret" not in str(caught.value)
    assert len(fake.attempts) == 1
    fake.sleep.assert_not_awaited()


def test_search_retries_transient_transport_failure(search_transport):
    import asyncio

    import httpx2

    fake = search_transport
    fake.outcomes.extend(
        [
            httpx2.ConnectError("fixture-secret"),
            TimeoutError("fixture-secret"),
            result({"sessionDefinitions": []}),
        ]
    )
    assert asyncio.run(fake.client.search("find_sessions", {"query": "test", "limit": 5})) == {
        "sessionDefinitions": []
    }
    assert len(fake.attempts) == 3
    assert [call.args[0] for call in fake.sleep.await_args_list] == [1, 2]


@pytest.mark.parametrize("status, expected_attempts", [(401, 1), (403, 1), (429, 3), (503, 3)])
def test_search_http_retry_policy_and_secret_safe_errors(
    search_transport, status, expected_attempts
):
    import asyncio

    import httpx2

    fake = search_transport
    request = httpx2.Request("GET", "https://example.test/fixture-secret")
    error = httpx2.HTTPStatusError(
        "fixture-secret", request=request, response=httpx2.Response(status, request=request)
    )
    fake.outcomes.extend([error] * expected_attempts)
    with pytest.raises(LogRocketMCPError) as caught:
        asyncio.run(fake.client.search("find_sessions", {"query": "test", "limit": 5}))
    assert "fixture-secret" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert len(fake.attempts) == expected_attempts


def test_search_bounds_total_timeout_and_withholds_details(search_transport, monkeypatch):
    import asyncio

    fake = search_transport

    @asynccontextmanager
    async def timeout(seconds):
        assert seconds == 100
        raise TimeoutError("fixture-secret")
        yield  # pragma: no cover

    monkeypatch.setattr(asyncio, "timeout", timeout)
    with pytest.raises(LogRocketMCPError, match="timed out") as caught:
        asyncio.run(fake.client.search("find_sessions", {"query": "test", "limit": 5}))
    assert "fixture-secret" not in str(caught.value)


def test_search_unknown_errors_fail_without_retry_and_hide_details(search_transport):
    import asyncio

    fake = search_transport
    fake.outcomes.extend([RuntimeError("fixture-secret")] * 3)
    with pytest.raises(LogRocketMCPError) as caught:
        asyncio.run(fake.client.search("find_sessions", {"query": "test", "limit": 5}))
    assert "RuntimeError" in str(caught.value)
    assert "fixture-secret" not in str(caught.value)
    assert len(fake.attempts) == 1


@pytest.mark.parametrize("status,attempts", [(401, 1), (429, 3)])
def test_nested_http_errors_classified(search_transport, status, attempts):
    import asyncio

    import httpx2

    request = httpx2.Request("GET", "https://example.test")
    error = httpx2.HTTPStatusError(
        "fixture-secret",
        request=request,
        response=httpx2.Response(status, request=request, headers={"Retry-After": "7"}),
    )
    fake = search_transport
    fake.outcomes.extend([ExceptionGroup("fixture-secret", [ExceptionGroup("inner", [error])])] * 3)
    with pytest.raises(LogRocketMCPError) as caught:
        asyncio.run(fake.client.search("find_sessions", {"query": "test", "limit": 5}))
    assert "fixture-secret" not in str(caught.value)
    assert len(fake.attempts) == attempts
    assert [call.args[0] for call in fake.sleep.await_args_list] == [7] * (attempts - 1)


def test_retry_after_beyond_deadline_does_not_retry_early(search_transport):
    import asyncio

    import httpx2

    request = httpx2.Request("GET", "https://example.test")
    fake = search_transport
    fake.outcomes.append(
        httpx2.HTTPStatusError(
            "fixture-secret",
            request=request,
            response=httpx2.Response(429, request=request, headers={"Retry-After": "1000"}),
        )
    )
    with pytest.raises(LogRocketMCPError, match="deadline"):
        asyncio.run(fake.client.search("find_sessions", {"query": "test", "limit": 5}))
    assert len(fake.attempts) == 1
    fake.sleep.assert_not_awaited()


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_json_nonfinite_constants_rejected(search_transport, constant):
    import asyncio

    fake = search_transport
    fake.outcomes.append(result(text='{"value":' + constant + "}"))
    with pytest.raises(LogRocketMCPError, match="malformed JSON"):
        asyncio.run(fake.client.search("find_sessions", {"query": "test", "limit": 5}))
    fake.sleep.assert_not_awaited()


def test_inspection_has_overall_deadline(monkeypatch):
    import asyncio

    import mcp
    import mcp.client.streamable_http as transport_module

    import cognee_community_connector_logrocket.client as client_module

    @asynccontextmanager
    async def connect(transport):
        await asyncio.Event().wait()
        yield

    monkeypatch.setattr(transport_module, "streamable_http_client", lambda *a, **kw: "offline")
    monkeypatch.setattr(mcp, "Client", connect)
    monkeypatch.setattr(client_module, "INSPECTION_TIMEOUT", 0.01)
    with pytest.raises(LogRocketMCPError, match="TimeoutError"):
        asyncio.run(LogRocketMCPClient("org", "project", api_key="fixture").inspect_capabilities())
