import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from examples.diagnostics import (
    LogRocketMCPError,
    _CapabilitySnapshot,
    probe_tool,
    response_shape,
    validate_request,
)

FIXTURE = Path(__file__).parent / "fixtures" / "live-required-tool-schemas-2026-10-16.json"


@pytest.fixture
def snapshot():
    return _CapabilitySnapshot.from_dict(json.loads(FIXTURE.read_text()))


def test_shape_excludes_values_and_preserves_empty_array_uncertainty():
    result = response_shape({"issues": [], "message": "private-user@example.test"})
    assert "private-user" not in json.dumps(result)
    assert result["fields"]["issues"]["item_shapes"] == []
    assert result["fields"]["issues"]["length"] == 0


def test_shape_retains_heterogeneous_array_types_without_values():
    result = response_shape([{"id": "private-id"}, None, True])
    assert len(result["item_shapes"]) == 3
    assert "private-id" not in json.dumps(result)


@pytest.mark.parametrize("limit", [None, 0, 6, True, "1"])
def test_probe_requires_small_explicit_limit(snapshot, limit):
    with pytest.raises(ValueError, match="explicit integer"):
        validate_request(snapshot, "find_sessions", {"query": "test", "limit": limit})


def test_probe_rejects_missing_required_field_without_echoing_values(snapshot):
    with pytest.raises(ValueError, match="input schema") as error:
        validate_request(snapshot, "find_sessions", {"limit": 1, "secret": "sensitive"})
    assert "sensitive" not in str(error.value)


def test_probe_rejects_replay_tool(snapshot):
    with pytest.raises(ValueError, match="Only find"):
        validate_request(snapshot, "watch_sessions", {"limit": 1})


def test_schema_drift_aborts_before_data_call(snapshot):
    changed_schema = dict(snapshot.tools[0].input_schema)
    changed_schema["properties"] = dict(changed_schema["properties"])
    changed_schema["properties"]["limit"] = {
        **changed_schema["properties"]["limit"],
        "maximum": 50,
    }
    changed = replace(snapshot.tools[0], input_schema=changed_schema)
    live = replace(snapshot, tools=(changed, *snapshot.tools[1:]))
    client = SimpleNamespace(call_tool=AsyncMock())
    with pytest.raises(ValueError, match="definition changed"):
        asyncio.run(probe_tool(client, live, snapshot, changed.name, {"query": "test", "limit": 1}))
    client.call_tool.assert_not_called()


def test_description_only_change_does_not_abort(snapshot):
    changed_schema = dict(snapshot.tools[0].input_schema)
    changed_schema["properties"] = dict(changed_schema["properties"])
    changed_schema["properties"]["query"] = {
        **changed_schema["properties"]["query"],
        "description": "Expanded documentation",
    }
    changed = replace(
        snapshot.tools[0], description="New tool documentation", input_schema=changed_schema
    )
    live = replace(snapshot, tools=(changed, *snapshot.tools[1:]))
    client = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                is_error=False,
                structured_content={"sessionDefinitions": []},
                content=[],
            )
        )
    )
    result = asyncio.run(
        probe_tool(client, live, snapshot, changed.name, {"query": "test", "limit": 1})
    )
    assert result["response_shape"]["fields"]["sessionDefinitions"]["length"] == 0


@pytest.mark.parametrize("structured", [True, False])
def test_probe_supports_mcp_encodings_without_assuming_record_fields(snapshot, structured):
    payload = {"unexpected_container": [{"unknown_id": "private-value"}]}
    client = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                is_error=False,
                structured_content=payload if structured else None,
                content=[]
                if structured
                else [SimpleNamespace(type="text", text=json.dumps(payload))],
            )
        )
    )
    result = asyncio.run(
        probe_tool(client, snapshot, snapshot, "find_sessions", {"query": "test", "limit": 1})
    )
    assert "private-value" not in json.dumps(result)
    assert result["sync_implementation_allowed"] is False
    assert "unexpected_container" in result["response_shape"]["fields"]
    client.call_tool.assert_awaited_once()


def test_tool_error_withholds_response(snapshot):
    client = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                is_error=True,
                content="secret-response",
            )
        )
    )
    with pytest.raises(LogRocketMCPError, match="content withheld") as error:
        asyncio.run(
            probe_tool(client, snapshot, snapshot, "find_sessions", {"query": "test", "limit": 1})
        )
    assert "secret-response" not in str(error.value)


def test_probe_against_official_sdk_in_process():
    from mcp import Client
    from mcp.server import MCPServer

    from examples.diagnostics import _collect_snapshot

    server = MCPServer("offline-response-probe")

    @server.tool()
    def find_sessions(query: str, limit: int) -> dict:
        return {"synthetic_records": [{"id": "do-not-persist"}]}

    async def run():
        async with Client(server) as client:
            snapshot = await _collect_snapshot(client, "in-process")
            return await probe_tool(
                client, snapshot, snapshot, "find_sessions", {"query": "test", "limit": 1}
            )

    result = asyncio.run(run())
    assert "do-not-persist" not in json.dumps(result)
    assert result["response_shape"]["fields"]["synthetic_records"]["length"] == 1
