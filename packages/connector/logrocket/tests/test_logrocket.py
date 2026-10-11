import asyncio
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
    _sync_scope,
    _sync_snapshot,
    _timestamp_ms,
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


def test_source_uses_real_mcp_client_and_project_scoped_url(tmp_path, monkeypatch):
    dlt = pytest.importorskip("dlt")
    from cognee_community_connector_logrocket import logrocket_source

    class ProjectScopedHTTP(FakeHTTPClient):
        def post(self, url, *, headers, json):
            response = super().post(url, headers=headers, json=json)
            if json["method"] == "tools/list":
                for tool in response._payload["result"]["tools"]:
                    tool["inputSchema"]["properties"].pop("organizationId")
                    tool["inputSchema"]["properties"].pop("projectId")
            return response

    transport = ProjectScopedHTTP()
    monkeypatch.setattr(
        "cognee_community_connector_logrocket.logrocket.httpx.Client",
        lambda **kwargs: transport,
    )
    pipeline = dlt.pipeline(
        pipeline_name="logrocket_project_scope_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'logrocket.sqlite'}"),
        dataset_name="logrocket_dataset",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        logrocket_source(
            api_key="secret",
            organization_id="org",
            project_id="project",
            resources=("issues",),
        )
    )

    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id FROM logrocket_issues") as cursor,
    ):
        assert cursor.fetchall() == [("org/project/issue/i1",)]
    assert transport.requests[0][0].endswith("/org/project?toolsets=issues")
    assert transport.requests[-1][2]["params"]["arguments"] == {}


def test_missing_tool_fails_before_call():
    transport = FakeHTTPClient()
    client = LogRocketMCPClient("secret", http_client=transport)
    with pytest.raises(LogRocketMCPError, match="unavailable"):
        client.tool_schema("missing")
    assert all(request[2]["method"] != "tools/call" for request in transport.requests)


def test_project_scoped_tool_can_have_no_input_arguments():
    client = LogRocketMCPClient("secret")
    client._initialized = True
    client._tools = {"find_issues": {"inputSchema": {"properties": {}}}}
    assert client.tool_schema("find_issues") == client._tools["find_issues"]


def test_scope_filter_cannot_override_project(tmp_path):
    dlt = pytest.importorskip("dlt")
    from cognee_community_connector_logrocket import logrocket_source

    pipeline = dlt.pipeline(
        pipeline_name="logrocket_scope_override_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'logrocket.sqlite'}"),
        dataset_name="logrocket_dataset",
        pipelines_dir=str(tmp_path / "state"),
    )
    with pytest.raises(Exception, match="scope fields cannot be overridden"):
        pipeline.run(
            logrocket_source(
                api_key="secret",
                organization_id="org",
                project_id="project",
                resources=("issues",),
                issue_filters={"projectId": "other"},
                client=FakeSourceClient(),
            )
        )


def test_sse_response_uses_last_json_message():
    response = FakeResponse(
        headers={"content-type": "text/event-stream"},
        text='event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":false}}\n\n'
        'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"ok":true}}\n\n',
    )
    assert _parse_response(response)["id"] == 2
    assert _parse_response(response, request_id=1)["id"] == 1
    with pytest.raises(LogRocketMCPError, match="omitted the request result"):
        _parse_response(response, request_id=3)


def test_mcp_tool_error_is_raised():
    transport = FakeHTTPClient()
    client = LogRocketMCPClient("secret", http_client=transport)
    client._request = lambda *args, **kwargs: {"isError": True, "content": []}
    with pytest.raises(LogRocketMCPError):
        list(client.iter_tool_records("find_sessions", {}, ("sessions",)))


def test_incomplete_page_without_cursor_fails():
    client = LogRocketMCPClient("secret")
    client._initialized = True
    client._tools = {
        "find_sessions": {
            "name": "find_sessions",
            "inputSchema": {"properties": {"cursor": {}}},
        }
    }
    client._request = lambda *args, **kwargs: {
        "structuredContent": {"sessions": [{"id": "s1"}], "hasMore": True}
    }

    with pytest.raises(LogRocketMCPError, match="without a pagination cursor"):
        list(client.iter_tool_records("find_sessions", {}, ("sessions",)))


def test_time_window_uses_schema_advertised_millisecond_fields():
    arguments = {}
    tool = {"name": "find_sessions", "inputSchema": {"properties": {"startMs": {}, "endMs": {}}}}

    _add_time_window(arguments, tool, "2026-01-01T00:00:00Z", 1767225600000)

    assert arguments == {"startMs": 1767225600000, "endMs": 1767225600000}


def test_time_window_cannot_silently_override_filter_and_naive_time_is_utc():
    tool = {"name": "find_sessions", "inputSchema": {"properties": {"startMs": {}}}}
    with pytest.raises(ValueError, match="specified twice"):
        _add_time_window({"startMs": 1}, tool, "2026-01-01T00:00:00Z", None)
    assert _timestamp_ms("2026-01-01T00:00:00") == _timestamp_ms("2026-01-01T00:00:00Z")


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


class MutableSourceClient:
    def __init__(self, sessions=None, issues=None):
        self.sessions = sessions or []
        self.issues = issues or []
        self.calls = []
        self.fail_after = None

    def tool_schema(self, tool_name):
        properties = {"organizationId": {}, "projectId": {}, "cursor": {}}
        if tool_name == "find_sessions":
            properties.update({"startMs": {}, "endMs": {}})
        return {"name": tool_name, "inputSchema": {"properties": properties}}

    def iter_tool_records(self, tool_name, arguments, record_keys):
        self.calls.append((tool_name, dict(arguments)))
        records = self.sessions if tool_name == "find_sessions" else self.issues
        for index, record in enumerate(records, 1):
            yield record
            if index == self.fail_after:
                raise LogRocketMCPError("listing stopped before the last page")


def test_snapshot_emits_only_changes_and_deletions():
    client = MutableSourceClient(sessions=[{"id": "old", "title": "Old"}, {"id": "new"}])
    state = {}
    scope = _sync_scope("org", "project", "url", "2026-01-01T00:00:00Z", None)

    def sync(end_time):
        return list(
            _sync_snapshot(
                client,
                "find_sessions",
                {},
                ("sessions",),
                lambda record: _session_to_row(record, "org", "project"),
                state,
                scope,
                end_time,
            )
        )

    first = sync("2026-01-02T00:00:00Z")
    assert {row["session_id"] for row in first} == {"old", "new"}
    assert all(row["_deleted"] is False for row in first)
    assert sync("2026-01-03T00:00:00Z") == []
    assert state["end_time_ms"] == 1767398400000

    client.sessions = [{"id": "new", "title": "Updated"}, {"id": "latest"}]
    third = sync("2026-01-04T00:00:00Z")
    assert {row["session_id"] for row in third if not row["_deleted"]} == {"new", "latest"}
    assert [row["id"] for row in third if row["_deleted"]] == ["org/project/session/old"]
    assert set(state["fingerprints"]) == {
        "org/project/session/new",
        "org/project/session/latest",
    }


def test_snapshot_rejects_scope_change_and_backwards_end_time():
    client = MutableSourceClient(sessions=[{"id": "s1"}])
    state = {}
    scope = _sync_scope("org", "project", "url", "2026-01-01T00:00:00Z", None)

    def convert(record):
        return _session_to_row(record, "org", "project")

    list(_sync_snapshot(client, "find_sessions", {}, ("sessions",), convert, state, scope, 2))

    changed_scope = _sync_scope("org", "project", "url", "2026-01-02T00:00:00Z", None)
    with pytest.raises(LogRocketMCPError, match="scope changed"):
        list(
            _sync_snapshot(
                client, "find_sessions", {}, ("sessions",), convert, state, changed_scope, 3
            )
        )
    with pytest.raises(LogRocketMCPError, match="backwards"):
        list(_sync_snapshot(client, "find_sessions", {}, ("sessions",), convert, state, scope, 1))
    assert len(client.calls) == 1


def test_partial_snapshot_keeps_previous_state():
    client = MutableSourceClient(sessions=[{"id": "s1"}, {"id": "s2"}])
    state = {}
    scope = _sync_scope("org", "project", "url", None, None)

    def convert(record):
        return _session_to_row(record, "org", "project")

    list(_sync_snapshot(client, "find_sessions", {}, ("sessions",), convert, state, scope))
    previous = dict(state)

    def fail_after_first(*args):
        yield {"id": "s1", "title": "Changed"}
        raise LogRocketMCPError("second page unavailable")

    client.iter_tool_records = fail_after_first
    with pytest.raises(LogRocketMCPError, match="second page unavailable"):
        list(_sync_snapshot(client, "find_sessions", {}, ("sessions",), convert, state, scope))
    assert state == previous


def test_duplicate_pages_are_deduplicated_but_conflicts_fail():
    client = MutableSourceClient(sessions=[{"id": "s1"}, {"id": "s1"}])
    state = {}
    scope = _sync_scope("org", "project", "url", None, None)

    def convert(record):
        return _session_to_row(record, "org", "project")

    first = list(_sync_snapshot(client, "find_sessions", {}, ("sessions",), convert, state, scope))
    assert len(first) == 1
    previous = dict(state)

    client.sessions = [{"id": "s1"}, {"id": "s1", "title": "different"}]
    with pytest.raises(LogRocketMCPError, match="conflicting records"):
        list(_sync_snapshot(client, "find_sessions", {}, ("sessions",), convert, state, scope))
    assert state == previous


def test_dlt_merge_keeps_old_rows_and_forgets_upstream_deletion(tmp_path):
    dlt = pytest.importorskip("dlt")
    from cognee_community_connector_logrocket import logrocket_source

    client = MutableSourceClient(sessions=[{"id": "old"}, {"id": "new"}])
    pipeline = dlt.pipeline(
        pipeline_name="logrocket_incremental_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'logrocket.sqlite'}"),
        dataset_name="logrocket_dataset",
        pipelines_dir=str(tmp_path / "state"),
    )

    def sync(end_time):
        pipeline.run(
            logrocket_source(
                api_key="test-key",
                organization_id="org",
                project_id="project",
                resources=("sessions",),
                start_time=1,
                end_time=end_time,
                client=client,
            ),
            write_disposition="merge",
            primary_key="id",
        )
        with pipeline.sql_client() as sql_client:
            rows = sql_client.execute_sql("SELECT id FROM logrocket_sessions ORDER BY id")
        return [row[0] for row in rows]

    assert sync(2) == ["org/project/session/new", "org/project/session/old"]
    assert sync(3) == ["org/project/session/new", "org/project/session/old"]
    client.fail_after = 1
    with pytest.raises(Exception, match="listing stopped before the last page"):
        sync(4)
    with pipeline.sql_client() as sql_client:
        retained = sql_client.execute_sql("SELECT id FROM logrocket_sessions ORDER BY id")
    assert [row[0] for row in retained] == [
        "org/project/session/new",
        "org/project/session/old",
    ]
    client.fail_after = None
    client.sessions = [{"id": "new"}, {"id": "latest"}]
    assert sync(4) == ["org/project/session/latest", "org/project/session/new"]
    assert [call[1]["startMs"] for call in client.calls] == [1, 1, 1, 1]


def test_session_window_does_not_filter_issue_reports(tmp_path):
    dlt = pytest.importorskip("dlt")
    from cognee_community_connector_logrocket import logrocket_source

    client = MutableSourceClient(sessions=[{"id": "s1"}], issues=[{"id": "i1"}])
    pipeline = dlt.pipeline(
        pipeline_name="logrocket_two_resources_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'logrocket.sqlite'}"),
        dataset_name="logrocket_dataset",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(
        logrocket_source(
            api_key="test-key",
            organization_id="org",
            project_id="project",
            resources=("sessions", "issues"),
            start_time=1,
            client=client,
        ),
        write_disposition="merge",
        primary_key="id",
    )
    assert "startMs" in next(args for name, args in client.calls if name == "find_sessions")
    assert "startMs" not in next(args for name, args in client.calls if name == "find_issues")


def test_cognee_pipeline_state_is_scoped_by_project():
    from cognee.tasks.ingestion.dlt_utils import pipeline_name_for_source

    from cognee_community_connector_logrocket import logrocket_source

    first = logrocket_source(api_key="fake", organization_id="org", project_id="one")
    second = logrocket_source(api_key="fake", organization_id="org", project_id="two")
    assert pipeline_name_for_source(first, "dataset") != pipeline_name_for_source(second, "dataset")
    assert pipeline_name_for_source(first, "dataset") != pipeline_name_for_source(first, "other")


def test_cognee_add_reconciles_deleted_and_unchanged_documents(tmp_path, monkeypatch):
    pytest.importorskip("dlt")
    import cognee
    from cognee.modules.data.methods import get_authorized_existing_datasets
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.users.methods import get_default_user
    from cognee.tasks.ingestion.dlt_utils import is_dlt_sourced

    from cognee_community_connector_logrocket import logrocket_source

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    client = MutableSourceClient(sessions=[{"id": "keep"}, {"id": "delete"}])
    dataset = "logrocket_integration_test"

    async def documents():
        user = await get_default_user()
        datasets = await get_authorized_existing_datasets(
            user=user, permission_type="write", datasets=[dataset]
        )
        all_data = await get_dataset_data(datasets[0].id)
        return {
            item.system_metadata["external_id"]: item
            for item in all_data
            if isinstance(item.system_metadata, dict)
            and item.system_metadata.get("source") == "logrocket"
        }

    async def sync():
        await cognee.add(
            logrocket_source(
                api_key="test-key",
                organization_id="org",
                project_id="project",
                resources=("sessions",),
                client=client,
            ),
            dataset_name=dataset,
            primary_key="id",
            write_disposition="merge",
            max_rows_per_table=0,
        )

    async def scenario():
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        try:
            await sync()
            first = await documents()
            assert set(first) == {"org/project/session/keep", "org/project/session/delete"}
            assert all(not is_dlt_sourced(item.system_metadata) for item in first.values())

            client.sessions = [{"id": "keep"}]
            await sync()
            second = await documents()
            assert set(second) == {"org/project/session/keep"}
            assert second["org/project/session/keep"].id == first["org/project/session/keep"].id
        finally:
            await cognee.prune.prune_data()
            await cognee.prune.prune_system(metadata=True)

    asyncio.run(scenario())
