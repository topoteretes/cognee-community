import json

import pytest

from cognee_community_connector_logrocket.logrocket import (
    LogRocketMCPClient,
    LogRocketMCPError,
    _add_time_window,
    _issue_to_row,
    _parse_response,
    _scope_arguments,
    _session_to_row,
)


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, headers=None, text=None):
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


class FakeHTTPClient:
    def __init__(self):
        self.requests = []

    def post(self, url, *, headers, json):
        self.requests.append((url, headers, json))
        method = json["method"]
        if method == "initialize":
            return FakeResponse(
                {"result": {"serverInfo": {"name": "logrocket"}}},
                headers={"content-type": "application/json", "Mcp-Session-Id": "session-1"},
            )
        if method == "notifications/initialized":
            return FakeResponse(status_code=202)
        if method == "tools/list":
            return FakeResponse(
                {
                    "result": {
                        "tools": [
                            {
                                "name": "find_sessions",
                                "inputSchema": {
                                    "properties": {
                                        "organizationId": {},
                                        "projectId": {},
                                        "cursor": {},
                                        "startMs": {},
                                        "endMs": {},
                                    }
                                },
                            },
                            {
                                "name": "find_issues",
                                "inputSchema": {
                                    "properties": {
                                        "organizationId": {},
                                        "projectId": {},
                                        "cursor": {},
                                    }
                                },
                            },
                        ]
                    }
                }
            )
        if method == "tools/call":
            tool_name = json["params"]["name"]
            arguments = json["params"]["arguments"]
            if tool_name == "find_sessions" and arguments.get("cursor"):
                payload = {"sessions": [{"id": "s2", "title": "Second"}]}
            elif tool_name == "find_sessions":
                payload = {"sessions": [{"id": "s1", "title": "First"}], "nextCursor": "next-1"}
            else:
                payload = {"issues": [{"id": "i1", "title": "Error"}]}
            return FakeResponse({"result": {"structuredContent": payload}})
        raise AssertionError(method)


def test_mcp_handshake_auth_and_pagination():
    transport = FakeHTTPClient()
    client = LogRocketMCPClient("secret", http_client=transport)

    records = list(
        client.iter_tool_records(
            "find_sessions",
            {"organizationId": "o", "projectId": "p"},
            ("sessions",),
        )
    )

    assert [record["id"] for record in records] == ["s1", "s2"]
    assert transport.requests[0][1]["Authorization"] == "Bearer secret"
    assert transport.requests[1][2]["method"] == "notifications/initialized"
    assert transport.requests[-1][1]["Mcp-Session-Id"] == "session-1"
    assert transport.requests[-1][2]["params"]["arguments"]["cursor"] == "next-1"


def test_sse_response_uses_last_json_message():
    response = FakeResponse(
        headers={"content-type": "text/event-stream"},
        text='event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":false}}\n\n'
        'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"ok":true}}\n\n',
    )
    assert _parse_response(response)["id"] == 2


def test_mcp_tool_error_is_raised():
    transport = FakeHTTPClient()
    client = LogRocketMCPClient("secret", http_client=transport)
    client._request = lambda *args, **kwargs: {"isError": True, "content": []}
    with pytest.raises(LogRocketMCPError):
        list(client.iter_tool_records("find_sessions", {}, ("sessions",)))


def test_time_window_uses_schema_advertised_millisecond_fields():
    arguments = {}
    tool = {"name": "find_sessions", "inputSchema": {"properties": {"startMs": {}, "endMs": {}}}}

    _add_time_window(arguments, tool, "2026-01-01T00:00:00Z", 1767225600000)

    assert arguments == {"startMs": 1767225600000, "endMs": 1767225600000}


def test_scope_arguments_follow_live_tool_schema_names():
    tool = {
        "name": "find_sessions",
        "inputSchema": {"properties": {"orgID": {}, "appID": {}}},
    }

    assert _scope_arguments(tool, "org", "project") == {"orgID": "org", "appID": "project"}


def test_time_window_without_supported_fields_fails_loudly():
    with pytest.raises(LogRocketMCPError, match="time-window field"):
        _add_time_window(
            {},
            {"name": "find_issues", "inputSchema": {"properties": {"cursor": {}}}},
            "2026-01-01T00:00:00Z",
            None,
        )


def test_session_row_excludes_replay_payload():
    row = _session_to_row(
        {"id": "s1", "title": "Checkout", "events": [{"secret": "data"}], "browser": "Chrome"},
        "org",
        "project",
    )

    assert row["id"] == "org/project/session/s1"
    assert "secret" not in row["content"]
    assert "secret" not in row["metadata"]
    assert "Chrome" in row["content"]


def test_issue_row_is_searchable_and_stable():
    row = _issue_to_row(
        {"id": "i1", "title": "Checkout failure", "issueType": "JS Error", "severity": "high"},
        "org",
        "project",
    )

    assert row["id"] == "org/project/issue/i1"
    assert "Checkout failure" in row["content"]
    assert row["issue_type"] == "JS Error"


def test_http_errors_are_not_hidden():
    class Unauthorized:
        def post(self, *args, **kwargs):
            return FakeResponse(status_code=401, headers={})

    client = LogRocketMCPClient("secret", http_client=Unauthorized())
    with pytest.raises(LogRocketMCPError, match="HTTP 401"):
        client._request("tools/list", {})


class FakeSourceClient:
    def tool_schema(self, tool_name):
        return {
            "name": tool_name,
            "inputSchema": {
                "properties": {"organizationId": {}, "projectId": {}, "cursor": {}},
            },
        }

    def iter_tool_records(self, tool_name, arguments, record_keys):
        assert arguments["organizationId"] == "org"
        assert arguments["projectId"] == "project"
        if tool_name == "find_issues":
            yield {"id": "issue-1", "title": "Checkout error"}
        else:
            yield {"id": "session-1", "title": "Checkout"}


def test_source_ingests_selected_resource_through_dlt(tmp_path):
    dlt = pytest.importorskip("dlt")
    from cognee_community_connector_logrocket import logrocket_source

    database = tmp_path / "logrocket.sqlite"
    pipeline = dlt.pipeline(
        pipeline_name="logrocket_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{database}"),
        dataset_name="logrocket_dataset",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        logrocket_source(
            api_key="test-key",
            organization_id="org",
            project_id="project",
            resources=("issues",),
            client=FakeSourceClient(),
        )
    )

    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title FROM logrocket_issues") as cursor,
    ):
        rows = cursor.fetchall()
    assert rows == [("org/project/issue/issue-1", "Checkout error")]
