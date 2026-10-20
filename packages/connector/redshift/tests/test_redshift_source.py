"""redshift_source through a real dlt pipeline (temp sqlite destination).

Checks what cognee reads back from staging: one table per selection plus the
table cards, tombstones that really remove rows on merge, and the guard
against any write disposition other than merge.
"""

import dlt
import pytest
from cognee.tasks.ingestion import dlt_utils
from fake_redshift import FakeRedshift

from cognee_community_connector_redshift import RedshiftTable, redshift_source


@pytest.fixture
def fake(tmp_path):
    fake = FakeRedshift(tmp_path)
    fake.create_table(
        "tickets",
        {"id": "INTEGER", "subject": "TEXT", "updated_at": "TIMESTAMP"},
        comment="Support tickets.",
    )
    fake.insert(
        "tickets",
        {"id": 1, "subject": "Login fails", "updated_at": "2026-01-01 10:00:00"},
        {"id": 2, "subject": "Refund", "updated_at": "2026-01-02 10:00:00"},
    )
    return fake


TICKETS = RedshiftTable("public.tickets", primary_key="id", cursor_column="updated_at")


def _pipeline(tmp_path):
    return dlt.pipeline(
        pipeline_name="redshift_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'stage.db').as_posix()}"),
        dataset_name="redshift_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run(tmp_path, fake, **kwargs):
    source = redshift_source([TICKETS], connection=fake.connection(), **kwargs)
    _pipeline(tmp_path).run(
        source, write_disposition="merge", primary_key="id", columns=dlt_utils.NODE_SET_COLUMN_HINT
    )
    return source


def _ids(tmp_path, table):
    with (
        _pipeline(tmp_path).sql_client() as client,
        client.execute_query(f"SELECT id FROM {table}") as cursor,
    ):
        return {row[0] for row in cursor.fetchall()}


def test_rows_and_cards_land_in_their_own_staging_tables(tmp_path, fake):
    source = _run(tmp_path, fake)

    assert _ids(tmp_path, "redshift_public_tickets") == {
        "redshift:public.tickets:1",
        "redshift:public.tickets:2",
    }
    assert _ids(tmp_path, "redshift_table_cards") == {"redshift:table:public.tickets"}
    assert source.cognee_sync_stats["rows"] == 2


def test_upstream_delete_removes_the_staged_row(tmp_path, fake):
    _run(tmp_path, fake)
    fake.delete("tickets", "id", 1)

    source = _run(tmp_path, fake)

    assert _ids(tmp_path, "redshift_public_tickets") == {"redshift:public.tickets:2"}
    assert source.cognee_sync_stats["deleted"] == 1


def test_cursor_survives_between_runs(tmp_path, fake):
    _run(tmp_path, fake)
    fake.statements.clear()

    _run(tmp_path, fake)

    params = [p for sql, p in fake.statements if ">= :cognee_cursor" in sql]
    assert params == [{"cognee_cursor": "2026-01-02 10:00:00"}]


def test_dropped_table_empties_its_staging_table(tmp_path, fake):
    _run(tmp_path, fake)
    fake.drop_table("tickets")

    _run(tmp_path, fake)

    assert _ids(tmp_path, "redshift_public_tickets") == set()
    assert _ids(tmp_path, "redshift_table_cards") == set()


def test_replace_disposition_is_refused(tmp_path, fake):
    source = redshift_source([TICKETS], connection=fake.connection())
    with pytest.raises(Exception, match='write_disposition="merge"'):
        _pipeline(tmp_path).run(source, write_disposition="replace")


def test_source_declares_cognee_markers(fake):
    source = redshift_source([TICKETS], connection=fake.connection(), resource_name="finance")

    assert dlt_utils.document_source_tag(source) == "redshift"
    assert getattr(source, dlt_utils.PIPELINE_SCOPE_ATTR) == "finance"
    assert list(source.resources) == ["finance"]


def test_resource_name_must_be_a_plain_name(fake):
    with pytest.raises(ValueError, match="resource_name"):
        redshift_source([TICKETS], connection=fake.connection(), resource_name="Finance Team")


def test_default_connection_is_the_data_api_from_the_environment(monkeypatch):
    monkeypatch.setenv("REDSHIFT_DATABASE", "dev")
    monkeypatch.setenv("REDSHIFT_WORKGROUP_NAME", "analytics")
    # Built without touching AWS: the client is created on the first statement.
    source = redshift_source([TICKETS])
    assert dlt_utils.document_source_tag(source) == "redshift"
