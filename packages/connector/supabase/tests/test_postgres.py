"""Real PostgreSQL extraction; not the Cognee graph/vector E2E test."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from test_sync_isolation import Sync

from cognee_community_connector_supabase import supabase_source

pytestmark = pytest.mark.integration


def pg_source(reader, project="a"):
    return supabase_source(
        reader,
        project_ref=project,
        schema=f"project_{project}",
        tables=["customers"],
        columns={"customers": ["id", "name"]},
        cursor_columns={"customers": "updated_at"},
        chunk_size=1,
    )


def test_postgres_readonly_selection_and_lifecycle(postgres_fixture, tmp_path):
    writer, reader = postgres_fixture
    with writer.begin() as db:
        for project in ("a", "b"):
            db.execute(
                text(
                    f"INSERT INTO project_{project}.customers VALUES "
                    "(1, :name, 'never-export-private', '2026-01-01'), "
                    "(2, :name, 'never-export-private', '2026-01-01')"
                ),
                {"name": f"project-{project}-initial"},
            )
    # Owner/writer is refused even with the session read-only setting.
    with pytest.raises(PermissionError, match="no write privileges"):
        pg_source(writer)
    # Validation marks that engine read-only; use a fresh preparation engine.
    preparation = create_engine(writer.url)
    sync = Sync(tmp_path)
    try:
        sync.pipeline.run(pg_source(reader), write_disposition="merge")
        preserved = sync.rows()
        with reader.connect() as db:
            assert db.execute(text("SHOW transaction_read_only")).scalar() == "on"
            for statement in (
                "INSERT INTO project_a.customers VALUES (99, 'bad', '', now())",
                "UPDATE project_a.customers SET name = 'bad'",
                "DELETE FROM project_a.customers",
                "TRUNCATE project_a.customers",
            ):
                with pytest.raises(DBAPIError):
                    db.execute(text(statement))
                db.rollback()
        sync.pipeline.run(pg_source(reader, "b"), write_disposition="merge")
        for step in ("update", "delete", "last", "empty"):
            with preparation.begin() as db:
                if step == "update":
                    db.execute(
                        text(
                            "UPDATE project_b.customers SET name='B-updated', "
                            "updated_at='2026-01-02' WHERE id=1"
                        )
                    )
                elif step == "delete":
                    db.execute(text("DELETE FROM project_b.customers WHERE id=2"))
                elif step == "last":
                    db.execute(text("DELETE FROM project_b.customers"))
            sync.restore()
            sync.pipeline.run(pg_source(reader, "b"), write_disposition="merge")
            rows = sync.rows()
            for name, contents in preserved.items():
                assert rows[name] == contents
            b_rows = next(contents for name, contents in rows.items() if name not in preserved)
            assert len(b_rows) == {"update": 2, "delete": 1, "last": 0, "empty": 0}[step]
            if step in ("update", "delete"):
                assert "B-updated" in str(b_rows)
            assert "never-export-private" not in str(rows)
            assert "private_note" not in str(rows)
    finally:
        preparation.dispose()
