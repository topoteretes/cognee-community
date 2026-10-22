from examples.diagnostics import (
    _CapabilitySnapshot,
    _ToolDefinition,
    inspect_snapshot,
)


def schema_backed_tool(name):
    singular = name.removeprefix("find_").removesuffix("s")
    return _ToolDefinition(
        name=name,
        description=f"List {singular}s",
        input_schema={
            "type": "object",
            "properties": {
                "cursor": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            f"{singular}_id": {"type": "string"},
                            "updated_at": {"type": "string"},
                            "deleted": {"type": "boolean"},
                        },
                    },
                },
                "next_cursor": {"type": ["string", "null"]},
            },
        },
    )


def snapshot(*tools):
    return _CapabilitySnapshot(
        endpoint="offline-test",
        protocol_version="test",
        server_info=None,
        server_capabilities={"tools": {}},
        tools=tuple(tools),
    )


def test_complete_schema_evidence_still_requires_live_behavior_checks():
    report = inspect_snapshot(
        snapshot(schema_backed_tool("find_sessions"), schema_backed_tool("find_issues"))
    )

    assert report.sync_implementation_allowed is False
    assert all(tool.pagination == "schema_evidence" for tool in report.tools)
    assert all(tool.deletion_visibility == "schema_evidence" for tool in report.tools)
    assert any("cursor progress" in blocker for blocker in report.blockers)
    assert any("deletions are observable" in blocker for blocker in report.blockers)


def test_limit_alone_does_not_get_mistaken_for_pagination():
    tool = _ToolDefinition(
        name="find_sessions",
        description="Find sessions",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
        output_schema={
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"session_id": {"type": "string"}},
                    },
                }
            },
        },
    )

    report = inspect_snapshot(snapshot(tool, schema_backed_tool("find_issues")))

    sessions = report.tools[0]
    assert sessions.pagination == "partial"
    assert report.sync_implementation_allowed is False


def test_missing_output_schema_does_not_assume_response_shape_or_deletion():
    undocumented = _ToolDefinition(
        name="find_sessions",
        description="Find sessions",
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
    )

    report = inspect_snapshot(snapshot(undocumented, schema_backed_tool("find_issues")))

    sessions = report.tools[0]
    assert sessions.response_schema is False
    assert sessions.stable_identity == "unverified"
    assert sessions.deletion_visibility == "unverified"
    assert any("no MCP outputSchema" in blocker for blocker in report.blockers)


def test_deletion_sounding_tool_name_is_not_evidence_without_schema():
    audit_tool = _ToolDefinition(
        name="audit_session_deletions",
        description="Name alone is not a contract",
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
    )
    sessions = _ToolDefinition(
        name="find_sessions",
        description="Find sessions",
        input_schema={"type": "object", "properties": {}},
        output_schema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
        },
    )

    report = inspect_snapshot(snapshot(sessions, schema_backed_tool("find_issues"), audit_tool))

    assert report.tools[0].deletion_visibility == "unverified"
    assert report.tools[0].deletion_evidence == ()


def test_snapshot_round_trip_preserves_raw_schemas():
    original = snapshot(schema_backed_tool("find_sessions"), schema_backed_tool("find_issues"))

    restored = _CapabilitySnapshot.from_dict(original.to_dict())

    assert restored == original
