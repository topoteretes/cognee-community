"""Source interface and real dlt merge/hard-delete contract tests."""

import sqlite3

import dlt
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR

from cognee_community_connector_read_ai import ConfigurationError, SyncLimits, read_ai_source
from cognee_community_connector_read_ai.auth import ISSUER, OAuthIdentity
from cognee_community_connector_read_ai.models import Snapshot
from cognee_community_connector_read_ai.read_ai import _make_source


class UnusedTokenProvider:
    def get_tokens(self, *, force_refresh=False):
        pytest.fail("Construction and prepared snapshot loading must not call OAuth")


def test_lazy_document_markers_and_delete_hint(tmp_path):
    source = read_ai_source(
        "connection-one", token_provider=UnusedTokenProvider(), state_dir=tmp_path
    )
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "read_ai"
    assert getattr(source, PIPELINE_SCOPE_ATTR)
    resource = source.resources[source._read_ai_table]
    schema = resource.compute_table_schema()
    assert schema["columns"]["id"]["primary_key"] is True
    assert schema["write_disposition"] == "merge"
    assert schema["columns"]["_deleted"]["hard_delete"] is True
    assert schema["columns"]["_deleted"]["data_type"] == "bool"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"meeting_ids": []},
        {"meeting_ids": "one"},
        {"meeting_ids": [1]},
        {"meeting_ids": ["one"], "start_time": "2026-01-01T00:00:00Z"},
        {"start_time": "2026-01-01T00:00:00"},
        {"start_time": "2026-02-01T00:00:00Z", "end_time": "2026-01-01T00:00:00Z"},
        {"include_summary": False, "include_transcript": False, "include_metrics": False},
        {"include_summary": "yes"},
    ],
)
def test_invalid_selection_is_rejected_without_network(tmp_path, kwargs):
    with pytest.raises(ConfigurationError):
        read_ai_source(
            "connection-one", token_provider=UnusedTokenProvider(), state_dir=tmp_path, **kwargs
        )


@pytest.mark.parametrize("connection_id", ["", " ", None, 0])
def test_invalid_connection_id(connection_id):
    with pytest.raises(ConfigurationError):
        read_ai_source(connection_id, token_provider=UnusedTokenProvider())


def test_credential_modes_are_exclusive(tmp_path):
    with pytest.raises(ConfigurationError):
        read_ai_source(
            "connection",
            credentials_path=tmp_path / "credentials",
            token_provider=UnusedTokenProvider(),
        )


def test_open_end_and_order_independent_ids_fingerprint(tmp_path):
    a = read_ai_source(
        "connection",
        token_provider=UnusedTokenProvider(),
        state_dir=tmp_path,
        start_time="2026-01-01T05:30:00+05:30",
    )
    b = read_ai_source(
        "connection",
        token_provider=UnusedTokenProvider(),
        state_dir=tmp_path,
        start_time="2026-01-01T00:00:00Z",
    )
    assert a._read_ai_config.fingerprint() == b._read_ai_config.fingerprint()
    assert a._read_ai_config.fingerprint()["selection"]["end_time_ms"] is None
    assert a._read_ai_table == b._read_ai_table
    c = read_ai_source(
        "connection", token_provider=UnusedTokenProvider(), meeting_ids=["b", "a", "a"]
    )
    d = read_ai_source("connection", token_provider=UnusedTokenProvider(), meeting_ids=["a", "b"])
    assert c._read_ai_config.fingerprint() == d._read_ai_config.fingerprint()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_meetings": 0},
        {"max_pages": 1.5},
        {"max_response_bytes": True},
        {"max_total_response_bytes": -1},
        {"max_duration_seconds": float("inf")},
    ],
)
def test_invalid_limits(kwargs):
    with pytest.raises(ConfigurationError):
        SyncLimits(**kwargs)


def test_real_dlt_changed_only_merge_and_final_hard_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")
    initial = read_ai_source("connection", token_provider=UnusedTokenProvider(), state_dir=tmp_path)
    config = initial._read_ai_config
    table = initial._read_ai_table
    sqlite_path = tmp_path / "staging.sqlite"
    pipeline = dlt.pipeline(
        pipeline_name="read_ai_contract_test",
        destination=dlt.destinations.sqlalchemy(credentials=f"sqlite:///{sqlite_path}"),
        dataset_name="staging",
        pipelines_dir=str(tmp_path / "pipelines"),
    )
    # SQLAlchemy's SQLite destination attaches a dataset-specific database.
    loaded_path = tmp_path / "staging__staging.sqlite"
    row = {
        "id": "meeting-a",
        "title": "Title",
        "content": "Summary one",
        "url": "",
        "_deleted": False,
    }

    def prepared(rows):
        snapshot = Snapshot(
            meetings={key: {} for key in rows},
            rows=rows,
            pending=[],
            identity=OAuthIdentity(ISSUER, "subject"),
        )
        return _make_source(config, table, table, snapshot=snapshot)

    pipeline.run(prepared({"meeting-a": row}), write_disposition="merge", primary_key="id")
    with sqlite3.connect(loaded_path) as connection:
        assert connection.execute(f'SELECT content FROM "{table}"').fetchall() == [("Summary one",)]
    same = pipeline.run(prepared({"meeting-a": row}), write_disposition="merge", primary_key="id")
    assert same.loads_ids == []
    changed = {**row, "content": "Summary two"}
    pipeline.run(prepared({"meeting-a": changed}), write_disposition="merge", primary_key="id")
    with sqlite3.connect(loaded_path) as connection:
        assert connection.execute(f'SELECT content FROM "{table}"').fetchall() == [("Summary two",)]
    pipeline.run(prepared({}), write_disposition="merge", primary_key="id")
    with sqlite3.connect(loaded_path) as connection:
        assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
    assert pipeline.state["sources"]["read_ai"]["resources"][table]["hashes"] == {}
