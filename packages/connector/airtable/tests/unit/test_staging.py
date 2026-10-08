"""The connector's real dlt resource runs against a persistent SQLite destination."""

from __future__ import annotations

import copy
import json

import dlt
import pytest
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_airtable import airtable_source
from tests.fakes import FakeAirtableSession, record, response, table

BASE = "appOne"
TABLE = "tblOne"
RECORD_ID = f"{BASE}/{TABLE}/record/recOne"
SCHEMA_ID = f"{BASE}/{TABLE}/schema"


@pytest.fixture
def stage(tmp_path):
    pipeline = dlt.pipeline(
        pipeline_name="airtable_acceptance",
        destination=dlt.destinations.sqlalchemy(credentials=f"sqlite:///{tmp_path}/staging.sqlite"),
        dataset_name="staging",
        pipelines_dir=str(tmp_path / "pipelines"),
    )
    yield pipeline
    if pipeline.is_active:
        pipeline.deactivate()


def run(stage, session, **kwargs):
    # Every run crosses the public interface with a fresh resource/factory instance.
    resource = airtable_source(base_id=BASE, token="private-test-pat", session=session, **kwargs)
    stage.run(resource, primary_key="id", write_disposition="merge")
    return resource


def stored_rows(stage, resource):
    table_name = stage.default_schema.naming.normalize_table_identifier(resource.name)
    with stage.sql_client() as client:
        name = client.make_qualified_table_name(table_name)
        rows = client.execute_sql(f"SELECT id, title, content, url FROM {name} ORDER BY id")
    return {row[0]: dict(zip(("id", "title", "content", "url"), row, strict=True)) for row in rows}


def connector_state(stage):
    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get("tables"), dict) and TABLE in value["tables"]:
                yield value
            else:
                for child in value.values():
                    yield from visit(child)

    matches = list(visit(stage.state["sources"]))
    assert len(matches) == 1
    return copy.deepcopy(matches[0])


def test_fresh_factories_persist_cursors_hashes_and_merge_updates(stage):
    session = FakeAirtableSession()
    resource = run(stage, session)
    first = stored_rows(stage, resource)
    state = connector_state(stage)
    resource = run(stage, session)
    assert stored_rows(stage, resource) == first
    assert connector_state(stage) == state
    assert "private-test-pat" not in json.dumps(stage.state, default=str)

    session.records[TABLE][0] = record(text="Cedar delivers cobalt lanterns.")
    resource = run(stage, session)
    stored = stored_rows(stage, resource)
    assert len(stored) == 2
    assert "cobalt lanterns" in stored[RECORD_ID]["content"]
    assert "violet teapots" not in stored[RECORD_ID]["content"]
    assert stored[SCHEMA_ID] == first[SCHEMA_ID]


def test_sqlite_hard_delete_of_final_record_leaves_schema_then_removes_schema(stage):
    session = FakeAirtableSession()
    resource = run(stage, session)
    assert set(stored_rows(stage, resource)) == {RECORD_ID, SCHEMA_ID}
    session.records[TABLE] = []
    resource = run(stage, session)
    assert set(stored_rows(stage, resource)) == {SCHEMA_ID}
    resource = run(stage, session)
    assert set(stored_rows(stage, resource)) == {SCHEMA_ID}
    resource = run(stage, session, include_schema=False)
    assert stored_rows(stage, resource) == {}


def test_deleted_table_hard_deletes_its_schema_and_every_record(stage):
    session = FakeAirtableSession(records={TABLE: [record(), record("recTwo")]})
    resource = run(stage, session, table_ids=[TABLE])
    assert len(stored_rows(stage, resource)) == 3
    session.schema = []
    resource = run(stage, session, table_ids=[TABLE])
    assert stored_rows(stage, resource) == {}
    resource = run(stage, session, table_ids=[TABLE])
    assert stored_rows(stage, resource) == {}


def test_deselection_keeps_retained_rows_and_state(stage):
    session = FakeAirtableSession(
        schema=[table(), table("tblTwo", "Warehouses")],
        records={TABLE: [record()], "tblTwo": [record("recTwo")]},
    )
    resource = run(stage, session)
    first = stored_rows(stage, resource)
    prior = connector_state(stage)["tables"]["tblTwo"]
    session.records["tblTwo"] = []
    resource = run(stage, session, table_ids=[TABLE])
    assert stored_rows(stage, resource) == first
    assert connector_state(stage)["tables"]["tblTwo"] == prior


def test_failed_later_table_extract_publishes_neither_partial_delta_nor_state(stage):
    session = FakeAirtableSession(
        schema=[table(), table("tblTwo", "Warehouses")],
        records={TABLE: [record()], "tblTwo": [record("recTwo")]},
    )
    resource = run(stage, session)
    first = stored_rows(stage, resource)
    prior = connector_state(stage)
    session.records[TABLE][0]["fields"]["fldText"] = "Pending unpublished fact."
    session.failures["/v0/appOne/tblTwo/recTwo/comments"] = [response({}, 403)]
    with pytest.raises(PipelineStepFailed):
        run(stage, session)
    assert stored_rows(stage, resource) == first
    assert connector_state(stage) == prior
    resource = run(stage, session)
    assert "Pending unpublished fact." in stored_rows(stage, resource)[RECORD_ID]["content"]


def test_empty_inventory_and_later_restored_old_record_work_across_pipeline_reload(stage):
    session = FakeAirtableSession()
    resource = run(stage, session, include_schema=False)
    assert set(stored_rows(stage, resource)) == {RECORD_ID}
    session.records[TABLE] = []
    resource = run(stage, session, include_schema=False)
    assert stored_rows(stage, resource) == {}
    reloaded = dlt.attach(
        stage.pipeline_name, pipelines_dir=stage.pipelines_dir, destination=stage.destination
    )
    session.records[TABLE] = [record(timestamp="2000-01-01T00:00:00.000Z")]
    resource = run(reloaded, session, include_schema=False)
    assert set(stored_rows(reloaded, resource)) == {RECORD_ID}
    reloaded.deactivate()


def test_cursor_and_temporary_attachment_changes_preserve_actual_staging_rows(stage):
    session = FakeAirtableSession()
    session.schema[0]["fields"].append(
        {"id": "fldAttachment", "name": "Report", "type": "multipleAttachments"}
    )
    attachment = {"id": "attOne", "filename": "report.pdf", "size": 17, "url": "https://temp/first"}
    session.records[TABLE][0]["fields"]["fldAttachment"] = [attachment]
    resource = run(stage, session)
    first = stored_rows(stage, resource)
    attachment["url"] = "https://temp/refreshed"
    session.records[TABLE][0]["fields"]["fldModified"] = "2026-10-05T00:00:00.000Z"
    resource = run(stage, session)
    assert stored_rows(stage, resource) == first
    attachment["size"] = 25
    resource = run(stage, session)
    assert stored_rows(stage, resource)[RECORD_ID] != first[RECORD_ID]
