"""Offline tests for the Braze connector."""

from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import NAMESPACE_OID, uuid5

import pytest
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_braze.braze import (
    _detail_to_row,
    _iter_summaries,
    _resolve_endpoint,
    braze_source,
    sync_braze_content,
)

ENDPOINT = "https://rest.iad-01.braze.com"


def _summary(item_id: str, edited: str, name: str | None = None) -> dict:
    return {"id": item_id, "last_edited": edited, "name": name or item_id, "tags": []}


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeBrazeSession:
    def __init__(self, campaigns=None, canvases=None, details=None):
        self.campaigns = campaigns or []
        self.canvases = canvases or []
        self.details = details or {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        path = urlparse(url).path
        self.calls.append((path, params, timeout))

        if path in ("/campaigns/list", "/canvas/list"):
            key = "campaigns" if path == "/campaigns/list" else "canvases"
            records = list(getattr(self, key))
            cursor = params.get("last_edit.time[gt]")
            if cursor:
                records = [item for item in records if item["last_edited"] > cursor]
            page = params.get("page", 0)
            start = page * 100
            return _Response({key: records[start : start + 100], "message": "success"})

        if path in ("/campaigns/details", "/canvas/details"):
            source_id = params.get("campaign_id") or params.get("canvas_id")
            return _Response(self.details[source_id])

        raise AssertionError(f"Unexpected Braze path: {path}")


def test_endpoint_accepts_full_host_and_instance_alias(monkeypatch):
    assert _resolve_endpoint("https://rest.fra-01.braze.eu/") == ("https://rest.fra-01.braze.eu")
    assert _resolve_endpoint(cluster="US-01") == ENDPOINT
    assert _resolve_endpoint(cluster="fra-02") == "https://rest.fra-02.braze.eu"

    monkeypatch.setenv("BRAZE_REST_ENDPOINT", "rest.jp-01.braze.com")
    assert _resolve_endpoint() == "https://rest.jp-01.braze.com"
    # An explicit argument wins over ambient configuration.
    assert _resolve_endpoint(cluster="EU-01") == "https://rest.fra-01.braze.eu"


def test_endpoint_rejects_missing_or_ambiguous_configuration(monkeypatch):
    monkeypatch.delenv("BRAZE_REST_ENDPOINT", raising=False)
    monkeypatch.delenv("BRAZE_CLUSTER", raising=False)
    with pytest.raises(ValueError, match="REST endpoint required"):
        _resolve_endpoint()
    with pytest.raises(ValueError, match="either rest_endpoint or cluster"):
        _resolve_endpoint(ENDPOINT, "US-01")


def test_list_pagination_reads_every_page():
    session = FakeBrazeSession(
        campaigns=[_summary(str(index), f"2024-01-01T00:{index:02d}:00Z") for index in range(101)]
    )

    records = list(_iter_summaries(session, ENDPOINT, "campaigns"))

    assert len(records) == 101
    list_calls = [call for call in session.calls if call[0] == "/campaigns/list"]
    assert [call[1]["page"] for call in list_calls] == [0, 1]


def test_initial_sync_fetches_details_and_records_cursor():
    summaries = [
        _summary("c1", "2024-01-01T00:00:00Z", "Welcome"),
        _summary("c2", "2024-01-02T00:00:00Z", "Winback"),
    ]
    session = FakeBrazeSession(
        campaigns=summaries,
        details={
            "c1": {"name": "Welcome", "messages": {}},
            "c2": {"name": "Winback", "messages": {}},
        },
    )
    state = {}

    rows = list(sync_braze_content(session, ENDPOINT, state, content_types=("campaigns",)))

    assert [row["id"] for row in rows] == ["campaign:c1", "campaign:c2"]
    assert state["campaigns"] == {
        "known_ids": ["c1", "c2"],
        "last_edited": "2024-01-02T00:00:00Z",
    }


def test_incremental_sync_fetches_only_changed_and_new_details():
    session = FakeBrazeSession(
        campaigns=[
            _summary("c1", "2024-01-01T00:00:00Z"),
            _summary("c2", "2024-02-01T00:00:00Z"),
            # New to state but deliberately behind the cursor: it must still load.
            _summary("c3", "2023-12-01T00:00:00Z"),
        ],
        details={"c2": {"name": "Changed"}, "c3": {"name": "New selection"}},
    )
    state = {
        "campaigns": {
            "known_ids": ["c1", "c2"],
            "last_edited": "2024-01-15T00:00:00Z",
        }
    }

    rows = list(sync_braze_content(session, ENDPOINT, state, content_types=("campaigns",)))

    assert {row["id"] for row in rows} == {"campaign:c2", "campaign:c3"}
    detail_ids = [
        call[1]["campaign_id"] for call in session.calls if call[0] == "/campaigns/details"
    ]
    assert detail_ids == ["c2", "c3"]
    filtered_call = next(call for call in session.calls if "last_edit.time[gt]" in call[1])
    assert filtered_call[1]["last_edit.time[gt]"] == "2024-01-15T00:00:00Z"


def test_incremental_no_changes_fetches_no_details():
    session = FakeBrazeSession(campaigns=[_summary("c1", "2024-01-01T00:00:00Z")])
    state = {
        "campaigns": {
            "known_ids": ["c1"],
            "last_edited": "2024-01-01T00:00:00Z",
        }
    }

    rows = list(sync_braze_content(session, ENDPOINT, state, content_types=("campaigns",)))

    assert rows == []
    assert not any(path == "/campaigns/details" for path, _, _ in session.calls)


def test_deleted_upstream_record_emits_hard_delete_marker():
    session = FakeBrazeSession(campaigns=[_summary("c1", "2024-01-01T00:00:00Z")])
    state = {
        "campaigns": {
            "known_ids": ["c1", "c2"],
            "last_edited": "2024-01-01T00:00:00Z",
        }
    }

    rows = list(sync_braze_content(session, ENDPOINT, state, content_types=("campaigns",)))

    assert rows == [{"id": "campaign:c2", "_deleted": True}]
    assert state["campaigns"]["known_ids"] == ["c1"]


def test_selection_limits_ingest_and_reconciliation_scope():
    session = FakeBrazeSession(
        campaigns=[
            _summary("c1", "2024-01-01T00:00:00Z"),
            _summary("c2", "2024-01-02T00:00:00Z"),
        ],
        canvases=[_summary("v1", "2024-01-03T00:00:00Z")],
        details={"c2": {"name": "Selected"}, "v1": {"name": "Not requested"}},
    )

    rows = list(
        sync_braze_content(
            session,
            ENDPOINT,
            {},
            content_types=("campaigns",),
            campaign_ids={"c2"},
        )
    )

    assert [row["id"] for row in rows] == ["campaign:c2"]
    assert not any(path.startswith("/canvas") for path, _, _ in session.calls)


def test_campaign_message_content_is_rendered_as_searchable_text():
    row = _detail_to_row(
        "campaigns",
        _summary("c1", "2024-01-01T00:00:00Z", "Welcome"),
        {
            "name": "Welcome",
            "description": "New user onboarding",
            "channels": ["email"],
            "messages": {
                "m1": {
                    "channel": "email",
                    "subject": "Hello {{first_name}}",
                    "body": "<p>Thanks for joining <b>Acme</b>.</p>",
                }
            },
        },
    )

    assert row["id"] == "campaign:c1"
    assert "Hello {{first_name}}" in row["content"]
    assert "Thanks for joining Acme." in row["content"]
    assert "<p>" not in row["content"]


def test_canvas_step_message_content_is_rendered():
    row = _detail_to_row(
        "canvases",
        _summary("v1", "2024-01-01T00:00:00Z", "Lifecycle"),
        {
            "name": "Lifecycle",
            "steps": [
                {
                    "id": "step-1",
                    "name": "Reminder",
                    "messages": {"m1": {"channel": "sms", "body": "Come back soon"}},
                }
            ],
        },
    )

    assert row["id"] == "canvas:v1"
    assert "Step: Reminder" in row["content"]
    assert "Come back soon" in row["content"]


def test_braze_row_becomes_a_searchable_document_data_item():
    row = SimpleNamespace(
        row_data={
            "id": "campaign:c1",
            "source_id": "c1",
            "content_type": "campaign",
            "title": "Welcome",
            "content": "Welcome to Acme",
        },
        content_hash="hash",
        table_name="braze_content",
    )
    data_id = uuid5(NAMESPACE_OID, "campaign:c1")

    item = _build_document_data_item(row, data_id, "braze")

    metadata = item.system_metadata if hasattr(item, "system_metadata") else item.external_metadata
    assert metadata["source"] == "braze"
    assert metadata["external_id"] == "campaign:c1"
    assert item.data_id == data_id
    assert "Welcome to Acme" in item.data


def test_source_is_document_mode_merge_with_hard_delete():
    pytest.importorskip("dlt")
    source = braze_source(
        api_key="test-key",
        rest_endpoint=ENDPOINT,
        content_types=("campaigns",),
        session=FakeBrazeSession(),
    )
    assert source.cognee_document_source == "braze"

    resource = source.resources["braze_content"]
    schema = resource.compute_table_schema()
    disposition = schema["write_disposition"]
    if isinstance(disposition, dict):
        disposition = disposition.get("disposition")
    assert disposition == "merge"
    assert schema["columns"]["id"]["primary_key"] is True
    assert schema["columns"]["_deleted"]["hard_delete"] is True


def test_source_validates_content_selection():
    pytest.importorskip("dlt")
    with pytest.raises(ValueError, match="Unsupported"):
        braze_source(
            api_key="key",
            rest_endpoint=ENDPOINT,
            content_types=("segments",),
            session=FakeBrazeSession(),
        )
    with pytest.raises(ValueError, match="at least one"):
        braze_source(
            api_key="key",
            rest_endpoint=ENDPOINT,
            content_types=(),
            session=FakeBrazeSession(),
        )


def test_ingest_and_forget_on_delete_through_real_dlt_merge(tmp_path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("sqlalchemy")
    database = (tmp_path / "braze.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="test_braze_e2e",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{database}"),
        dataset_name="braze",
        pipelines_dir=str(tmp_path / "state"),
    )

    first_session = FakeBrazeSession(
        campaigns=[
            _summary("c1", "2024-01-01T00:00:00Z"),
            _summary("c2", "2024-01-02T00:00:00Z"),
        ],
        details={"c1": {"name": "One"}, "c2": {"name": "Two"}},
    )
    pipeline.run(
        braze_source(
            api_key="key",
            rest_endpoint=ENDPOINT,
            content_types=("campaigns",),
            session=first_session,
        )
    )

    with pipeline.sql_client() as client:
        assert client.execute_sql("SELECT count(*) FROM braze_content")[0][0] == 2

    # The cursor makes c1 a no-op, while the missing c2 produces a hard-delete row.
    second_session = FakeBrazeSession(campaigns=[_summary("c1", "2024-01-01T00:00:00Z")])
    pipeline.run(
        braze_source(
            api_key="key",
            rest_endpoint=ENDPOINT,
            content_types=("campaigns",),
            session=second_session,
        )
    )

    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id FROM braze_content")
    assert rows == [("campaign:c1",)]
    assert not any(path == "/campaigns/details" for path, _, _ in second_session.calls)
