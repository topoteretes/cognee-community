"""basecamp_source through a real dlt pipeline into a temp sqlite destination.

This is the path cognee takes: rows land in a dlt table with merge +
hard-delete, and whatever is no longer in that table is forgotten by cognee's
orphan cleanup. So "gone from the table" here means "forgotten" in cognee.
"""

import httpx
import pytest

from cognee_community_connector_basecamp import basecamp_source

dlt = pytest.importorskip("dlt")

UA = "cognee-basecamp-tests (test@example.com)"
TABLE = "basecamp_999_recordings"


def _source(fake, **kwargs):
    return basecamp_source(
        "999",
        access_token="token",
        user_agent=UA,
        http_client=fake.http_client(),
        full_sync_every=0,
        **kwargs,
    )


def _pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="basecamp_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'bc.db').as_posix()}"),
        dataset_name="basecamp_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _rows(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, title, content FROM {TABLE}") as cursor,
    ):
        return {row[0]: {"title": row[1], "content": row[2]} for row in cursor.fetchall()}


def _doc_id(fake):
    return next(r["id"] for r in fake.recordings.values() if r["type"] == "Document")


def test_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    source = basecamp_source("999", access_token="t", user_agent=UA)
    assert document_source_tag(source) == "basecamp"


def test_source_has_its_own_pipeline_scope():
    from cognee.tasks.ingestion.dlt_utils import PIPELINE_SCOPE_ATTR, pipeline_name_for_source

    a = basecamp_source("999", access_token="t", user_agent=UA)
    b = basecamp_source("888", access_token="t", user_agent=UA)
    assert getattr(a, PIPELINE_SCOPE_ATTR) == TABLE
    # Each account gets its own dlt pipeline, never the shared default one.
    assert pipeline_name_for_source(a, "ds") != "ingest_dlt_source"
    assert pipeline_name_for_source(a, "ds") != pipeline_name_for_source(b, "ds")


def test_source_refuses_cognee_without_per_row_node_sets(monkeypatch):
    from cognee.tasks.ingestion import dlt_utils

    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 1)
    with pytest.raises(RuntimeError, match="DOCUMENT_SYNC_VERSION"):
        basecamp_source("999", access_token="t", user_agent=UA)


def test_sync_stats_report_counts_only(seeded, tmp_path):
    pipeline = _pipeline(tmp_path)
    source = _source(seeded)
    pipeline.run(source)
    assert source.cognee_sync_stats == {"scanned": 6, "changed": 6, "deleted": 0, "skipped": 0}

    doc = _doc_id(seeded)
    seeded.set_status(doc, "trashed")
    source = _source(seeded)
    pipeline.run(source)
    assert source.cognee_sync_stats["deleted"] == 1
    assert all(isinstance(v, int) for v in source.cognee_sync_stats.values())


def test_check_active_can_stop_a_sync(seeded, tmp_path):
    calls = []

    def check_active():
        calls.append(1)
        if len(calls) > 3:
            raise PermissionError("credentials revoked")

    pipeline = _pipeline(tmp_path)
    with pytest.raises(Exception, match="credentials revoked"):
        pipeline.run(_source(seeded, check_active=check_active))
    assert len(calls) == 4


def test_source_requires_user_agent_and_token(monkeypatch):
    for var in ("BASECAMP_USER_AGENT", "BASECAMP_ACCESS_TOKEN", "BASECAMP_REFRESH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError, match="user_agent"):
        basecamp_source("999", access_token="t")
    with pytest.raises(ValueError, match="access token"):
        basecamp_source("999", user_agent=UA)
    with pytest.raises(ValueError, match="Unsupported"):
        basecamp_source("999", access_token="t", user_agent=UA, types=["Upload"])


def test_first_sync_loads_all_items(seeded, tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded))
    rows = _rows(pipeline)
    assert len(rows) == 6
    assert any("The payments team owns checkout." in r["content"] for r in rows.values())


def test_edit_replaces_the_row_on_resync(seeded, tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded))
    doc = _doc_id(seeded)
    seeded.edit(doc, content='<p dir="auto">Start with the prod setup.</p>')
    pipeline.run(_source(seeded))
    content = _rows(pipeline)[f"document:{doc}"]["content"]
    assert "prod setup" in content
    assert "staging" not in content


def test_trashed_item_is_removed_from_the_table(seeded, tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded))
    doc = _doc_id(seeded)
    seeded.set_status(doc, "trashed")
    pipeline.run(_source(seeded))
    rows = _rows(pipeline)
    assert f"document:{doc}" not in rows
    assert len(rows) == 5


def test_purged_item_is_removed_after_a_sweep(seeded, tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded))
    doc = _doc_id(seeded)
    seeded.set_status(doc, "trashed")
    seeded.purge(doc)

    pipeline.run(_source(seeded))
    assert f"document:{doc}" in _rows(pipeline)  # incremental run cannot see a purge

    pipeline.run(_source(seeded, full_sync=True))
    assert f"document:{doc}" not in _rows(pipeline)


def test_failed_run_changes_nothing(seeded, tmp_path, monkeypatch):
    monkeypatch.setattr("cognee_community_connector_basecamp.basecamp.time.sleep", lambda s: None)
    pipeline = _pipeline(tmp_path)
    pipeline.run(_source(seeded))
    before = _rows(pipeline)
    doc = _doc_id(seeded)
    seeded.set_status(doc, "trashed")

    def broken(request):
        return httpx.Response(500)

    failing = basecamp_source(
        "999",
        access_token="token",
        user_agent=UA,
        http_client=httpx.Client(transport=httpx.MockTransport(broken)),
        full_sync_every=0,
    )
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the BasecampError
        pipeline.run(failing)
    assert _rows(pipeline) == before

    # The cursor did not move, so the next good run still sees the trash.
    pipeline.run(_source(seeded))
    assert f"document:{doc}" not in _rows(pipeline)
