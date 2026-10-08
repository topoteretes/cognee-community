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
