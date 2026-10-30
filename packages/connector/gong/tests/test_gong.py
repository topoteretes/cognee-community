"""Gong connector tests without a tenant or external services."""

from datetime import UTC, datetime

import httpx
import pytest

from cognee_community_connector_gong.gong import GongClient, _call_row, sync_calls


def _call(call_id, started="2026-09-30T10:00:00Z", title=None):
    return {
        "id": call_id,
        "started": started,
        "title": title or f"Call {call_id}",
        "url": f"https://app.gong.io/call?id={call_id}",
        "duration": 60,
    }


class FakeGongClient:
    base_url = "https://tenant.api.gong.io"

    def __init__(self, calls):
        self.calls = calls
        self.details = {}
        self.transcript_data = {}
        self.list_windows = []
        self.detail_batches = []
        self.transcript_batches = []
        self.fail_inventory = False

    def list_calls(self, from_datetime, to_datetime, workspace_id=None):
        self.list_windows.append((from_datetime, to_datetime, workspace_id))
        if self.fail_inventory:
            raise RuntimeError("Gong inventory failed")
        start = datetime.fromisoformat(from_datetime.replace("Z", "+00:00"))
        end = datetime.fromisoformat(to_datetime.replace("Z", "+00:00"))
        return [
            call
            for call in self.calls.values()
            if start <= datetime.fromisoformat(call["started"].replace("Z", "+00:00")) < end
        ]

    def call_details(self, call_ids, workspace_id=None):
        self.detail_batches.append(call_ids)
        return {
            call_id: self.details.get(call_id, {"metaData": {"id": call_id}})
            for call_id in call_ids
        }

    def transcripts(self, call_ids, workspace_id=None):
        self.transcript_batches.append(call_ids)
        return {
            call_id: self.transcript_data[call_id]
            for call_id in call_ids
            if call_id in self.transcript_data
        }


def _run(client, state, day, **kwargs):
    from_datetime = kwargs.pop("from_datetime", "2020-01-01T00:00:00Z")
    return list(
        sync_calls(
            client,
            state,
            from_datetime=from_datetime,
            now=datetime(2026, 10, day, tzinfo=UTC),
            **kwargs,
        )
    )


def test_ingest_renders_transcript_and_deal_context():
    client = FakeGongClient({"1": _call("1")})
    client.details["1"] = {
        "metaData": {"id": "1"},
        "context": [
            {
                "system": "Salesforce",
                "objects": [
                    {
                        "objectType": "Opportunity",
                        "objectId": "deal-1",
                        "fields": [
                            {"name": "Name", "value": "Renewal"},
                            {"name": "StageName", "value": "Negotiation"},
                        ],
                    }
                ],
            }
        ],
        "parties": [{"speakerId": "5", "name": "Ada"}],
    }
    client.transcript_data["1"] = {
        "callId": "1",
        "transcript": [{"speakerId": "5", "sentences": [{"text": "We need a renewal quote."}]}],
    }
    state = {}

    rows = _run(client, state, 1)

    assert [row["id"] for row in rows] == ["1"]
    assert rows[0]["title"] == "Call 1"
    assert "Deal: Renewal" in rows[0]["content"]
    assert "StageName: Negotiation" in rows[0]["content"]
    assert "Ada: We need a renewal quote." in rows[0]["content"]
    assert rows[0]["_deleted"] is False
    assert state["cursor"] == "2026-10-01T00:00:00Z"


def test_incremental_cursor_and_forget_on_delete():
    old = _call("old", started="2020-05-01T00:00:00Z")
    recent = _call("recent")
    client = FakeGongClient({"old": old, "recent": recent})
    state = {}
    assert {row["id"] for row in _run(client, state, 1)} == {"old", "recent"}

    client.detail_batches.clear()
    client.calls.pop("recent")  # upstream deletion
    client.calls["new"] = _call("new", started="2026-10-01T11:00:00Z")
    rows = _run(client, state, 2)

    assert rows == [
        _call_row(client.calls["new"], {"metaData": {"id": "new"}}, {}),
        {"id": "recent", "_deleted": True},
    ]
    assert client.detail_batches == [["new"]]  # old call not re-fetched
    assert client.list_windows[-1][0] == "2026-09-24T00:00:00Z"
    assert set(state["known_calls"]) == {"old", "new"}
    assert state["cursor"] == "2026-10-02T00:00:00Z"


def test_old_call_metadata_change_is_refetched_outside_window():
    client = FakeGongClient({"old": _call("old", started="2020-05-01T00:00:00Z")})
    state = {}
    _run(client, state, 1)
    client.calls["old"]["title"] = "Updated title"

    rows = _run(client, state, 2)

    assert [row["title"] for row in rows] == ["Updated title"]
    assert client.detail_batches[-1] == ["old"]


def test_scope_selection_and_failure_do_not_advance_cursor():
    client = FakeGongClient({"1": _call("1"), "2": _call("2")})
    state = {}
    assert [row["id"] for row in _run(client, state, 1, call_ids=["1"])] == ["1"]
    original_state = state.copy()
    client.fail_inventory = True
    with pytest.raises(RuntimeError, match="inventory failed"):
        _run(client, state, 2, call_ids=["1"])
    assert state == original_state
    client.fail_inventory = False
    assert _run(client, state, 2, call_ids=["2"])[-1] == {"id": "1", "_deleted": True}


def test_missing_call_details_aborts_without_advancing_cursor():
    client = FakeGongClient({"1": _call("1")})
    client.call_details = lambda call_ids, workspace_id=None: {}
    state = {}

    with pytest.raises(ValueError, match="omitted call details"):
        _run(client, state, 1)

    assert state == {}


def test_bad_dates_are_rejected():
    client = FakeGongClient({})
    with pytest.raises(ValueError, match="timezone"):
        _run(client, {}, 1, from_datetime="2020-01-01")


def test_api_pagination_auth_and_cursor_contract():
    requests = []

    def handler(request):
        requests.append(request)
        cursor = request.url.params.get("cursor")
        if cursor:
            return httpx.Response(200, json={"records": {}, "calls": [_call("2")]})
        return httpx.Response(200, json={"records": {"cursor": "next"}, "calls": [_call("1")]})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        client = GongClient(
            "https://tenant.api.gong.io",
            access_key="key",
            access_key_secret="secret",
            http_client=http_client,
        )
        assert [
            call["id"] for call in client.list_calls("2026-01-01T00:00:00Z", "2026-10-01T00:00:00Z")
        ] == ["1", "2"]
    assert requests[0].headers["Authorization"].startswith("Basic ")
    assert requests[1].url.params["cursor"] == "next"
    assert requests[1].url.params["fromDateTime"] == requests[0].url.params["fromDateTime"]


def test_api_rejects_incomplete_page_and_retries_rate_limit():
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"records": {}, "calls": [_call("1")]}),
        ]
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: next(responses))
    ) as http_client:
        client = GongClient(
            "https://tenant.api.gong.io", access_token="token", http_client=http_client
        )
        assert [
            call["id"] for call in client.list_calls("2026-01-01T00:00:00Z", "2026-10-01T00:00:00Z")
        ] == ["1"]
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"calls": []}))
    ) as http_client:
        client = GongClient(
            "https://tenant.api.gong.io", access_token="token", http_client=http_client
        )
        with pytest.raises(ValueError, match="Incomplete Gong"):
            list(client.list_calls("2026-01-01T00:00:00Z", "2026-10-01T00:00:00Z"))
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"records": {"totalRecords": 2}, "calls": [_call("1")]}
            )
        )
    ) as http_client:
        client = GongClient(
            "https://tenant.api.gong.io", access_token="token", http_client=http_client
        )
        with pytest.raises(ValueError, match="pagination"):
            list(client.list_calls("2026-01-01T00:00:00Z", "2026-10-01T00:00:00Z"))


def test_api_requests_transcripts_and_deal_context_by_call_id():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/extensive"):
            return httpx.Response(
                200,
                json={"records": {}, "calls": [{"metaData": {"id": "1"}, "context": []}]},
            )
        return httpx.Response(
            200,
            json={"records": {}, "callTranscripts": [{"callId": "1", "transcript": []}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = GongClient(
            "https://tenant.api.gong.io", access_token="token", http_client=http_client
        )
        assert set(client.call_details(["1"], workspace_id="space")) == {"1"}
        assert set(client.transcripts(["1"], workspace_id="space")) == {"1"}

    assert all(request.headers["Authorization"] == "Bearer token" for request in requests)
    assert all(request.method == "POST" for request in requests)
    assert all(request.content for request in requests)
    assert b'"callIds":["1"]' in requests[0].content
    assert b'"workspaceId":"space"' in requests[0].content
    assert b'"context":"Extended"' in requests[0].content


def test_api_auth_and_url_validation():
    with pytest.raises(ValueError, match="HTTPS origin"):
        GongClient("http://tenant.api.gong.io", access_token="token")
    with pytest.raises(ValueError, match="either"):
        GongClient(
            "https://tenant.api.gong.io",
            access_token="token",
            access_key="key",
            access_key_secret="secret",
        )


def test_dlt_merge_removes_deleted_call_and_keeps_cursor(tmp_path):
    """A real dlt merge must remove tombstoned calls from staging."""
    dlt = pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR, document_source_tag

    from cognee_community_connector_gong import gong_source

    calls = {
        "1": _call("1", started="2020-05-01T00:00:00Z"),
        "2": _call("2", started="2020-05-02T00:00:00Z"),
    }
    api = FakeGongClient(calls)
    db_path = (tmp_path / "gong.db").as_posix()

    def run():
        pipeline = dlt.pipeline(
            pipeline_name="gong_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
            dataset_name="gong_ds",
            pipelines_dir=str(tmp_path / "state"),
        )
        source = gong_source(from_datetime="2019-01-01T00:00:00Z", client=api)
        assert document_source_tag(source) == "gong"
        assert getattr(source, PIPELINE_SCOPE_ATTR) == "gong"
        pipeline.run(source, write_disposition="merge")
        with pipeline.sql_client() as sql_client:
            rows = sql_client.execute_sql("SELECT id FROM gong_calls ORDER BY id")
        return [row[0] for row in rows]

    assert run() == ["1", "2"]
    api.calls.pop("1")
    assert run() == ["2"]
    assert len(api.list_windows) == 3  # first inventory, then inventory + cursor window


def test_dlt_failed_later_batch_does_not_load_partial_sync(tmp_path):
    dlt = pytest.importorskip("dlt")
    from dlt.pipeline.exceptions import PipelineStepFailed

    from cognee_community_connector_gong import gong_source

    api = FakeGongClient({"old": _call("old", started="2020-05-01T00:00:00Z")})
    pipeline = dlt.pipeline(
        pipeline_name="gong_partial_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'gong.db').as_posix()}"),
        dataset_name="gong_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(gong_source(from_datetime="2019-01-01T00:00:00Z", client=api))
    api.calls = {
        str(index): _call(str(index), started="2020-05-02T00:00:00Z") for index in range(101)
    }
    original_details = api.call_details
    attempts = 0

    def fail_second_batch(call_ids, workspace_id=None):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise RuntimeError("Gong details failed")
        return original_details(call_ids, workspace_id)

    api.call_details = fail_second_batch
    with pytest.raises(PipelineStepFailed, match="Gong details failed"):
        pipeline.run(gong_source(from_datetime="2019-01-01T00:00:00Z", client=api))
    with pipeline.sql_client() as sql_client:
        rows = sql_client.execute_sql("SELECT id FROM gong_calls")
    assert [row[0] for row in rows] == ["old"]
