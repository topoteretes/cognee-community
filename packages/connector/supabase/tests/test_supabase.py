from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.exc import OperationalError

from cognee_community_connector_supabase.supabase import (
    SupabaseManagementAPIError,
    _document_row,
    create_supabase_authorization_url,
    discover_supabase_projects,
    discover_supabase_schema,
    exchange_supabase_oauth_code,
    supabase_source,
)


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self.response


def _database(tmp_path, filename="source.db"):
    engine = create_engine(f"sqlite:///{tmp_path / filename}")
    metadata = MetaData()
    Table(
        "customers",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String),
        Column("private_note", String),
        Column("updated_at", DateTime, nullable=False),
    )
    metadata.create_all(engine)
    return engine


def test_pkce_authorization_url_contains_required_parameters():
    authorization = create_supabase_authorization_url(
        "client-id", "https://example.test/callback", state="expected-state"
    )
    query = parse_qs(urlparse(authorization.url).query)

    assert query["response_type"] == ["code"]
    assert query["scope"] == ["projects:read"]
    assert query["state"] == ["expected-state"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    assert authorization.code_verifier not in authorization.url


def test_token_exchange_uses_pkce_and_hides_tokens_from_repr():
    session = _Session(
        _Response(200, {"access_token": "access-secret", "refresh_token": "refresh-secret"})
    )
    tokens = exchange_supabase_oauth_code(
        "client", "secret", "https://callback", "code", "verifier", session=session
    )

    _, _, request = session.calls[0]
    assert request["auth"] == ("client", "secret")
    assert request["data"]["code_verifier"] == "verifier"
    assert "access-secret" not in repr(tokens)
    assert "refresh-secret" not in repr(tokens)


def test_project_discovery_maps_response_and_sanitizes_errors():
    session = _Session(
        _Response(
            200,
            [
                {
                    "ref": "abc",
                    "name": "Demo",
                    "organization_id": "org",
                    "region": "eu",
                    "database": {"host": "db.abc.supabase.co"},
                }
            ],
        )
    )
    projects = discover_supabase_projects("token", session=session)
    assert projects[0].ref == "abc"
    assert projects[0].database_host == "db.abc.supabase.co"
    assert session.calls[0][2]["headers"] == {"Authorization": "Bearer token"}

    with pytest.raises(SupabaseManagementAPIError, match="HTTP 401") as error:
        discover_supabase_projects("very-secret", session=_Session(_Response(401, {"token": "x"})))
    assert "very-secret" not in str(error.value)


def test_schema_discovery_reports_columns_and_primary_key(tmp_path):
    engine = _database(tmp_path)
    discovered = discover_supabase_schema(engine, schema="main", enforce_read_only=False)

    assert discovered["customers"]["primary_key"] == ["id"]
    assert discovered["customers"]["columns"] == [
        "id",
        "name",
        "private_note",
        "updated_at",
    ]


def test_document_row_is_deterministic_and_respects_column_allow_list():
    first = _document_row(
        {"id": 7, "name": "Ada", "secret": "do not export"},
        project_ref="project",
        schema="public",
        table="customers",
        columns=["id", "name"],
        primary_key=["id"],
    )
    second = _document_row(
        {"name": "Ada", "id": 7, "secret": "changed"},
        project_ref="project",
        schema="public",
        table="customers",
        columns=["id", "name"],
        primary_key=["id"],
    )

    assert first == second
    assert first["id"] == 'supabase:project:public:customers:{"id":7}'
    assert "secret" not in first["content"]


def test_source_has_project_scoped_document_marker(tmp_path):
    source = supabase_source(
        _database(tmp_path),
        project_ref="project",
        schema="main",
        tables=["customers"],
        columns={"customers": ["id", "name", "updated_at"]},
        cursor_columns={"customers": "updated_at"},
        enforce_read_only=False,
    )

    assert source.cognee_document_source == "supabase:project:main:supabase"
    assert "supabase_deletion_sweep" in source.resources
    assert any(name.startswith("map_customers_") for name in source.resources)


def test_source_supports_multiple_explicitly_selected_tables(tmp_path):
    engine = _database(tmp_path)
    metadata = MetaData()
    Table(
        "orders",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("amount", Integer),
        Column("updated_at", DateTime, nullable=False),
    )
    metadata.create_all(engine)

    source = supabase_source(
        engine,
        project_ref="project",
        schema="main",
        tables=["customers", "orders"],
        columns={
            "customers": ["id", "name"],
            "orders": ["id", "amount"],
        },
        cursor_columns={"customers": "updated_at", "orders": "updated_at"},
        enforce_read_only=False,
    )

    mapped_resources = [name for name in source.resources if name.startswith("map_")]
    assert len(mapped_resources) == 2
    assert any(name.startswith("map_customers_") for name in mapped_resources)
    assert any(name.startswith("map_orders_") for name in mapped_resources)


def test_schema_discovery_propagates_connection_failure(tmp_path):
    unavailable = create_engine(f"sqlite:///{tmp_path / 'missing' / 'source.db'}")
    with pytest.raises(OperationalError, match="unable to open database file"):
        discover_supabase_schema(unavailable, schema="main", enforce_read_only=False)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"tables": ["missing"]}, "Unknown selected table"),
        ({"columns": {"customers": ["missing"]}}, "Unknown column"),
        ({"cursor_columns": {}}, "cursor_columns"),
    ],
)
def test_source_rejects_unsafe_or_invalid_selection(tmp_path, kwargs, message):
    engine = _database(tmp_path)
    options = {
        "project_ref": "project",
        "schema": "main",
        "tables": ["customers"],
        "columns": {"customers": ["id", "name", "updated_at"]},
        "cursor_columns": {"customers": "updated_at"},
        "enforce_read_only": False,
    }
    options.update(kwargs)
    if kwargs.get("tables") == ["missing"]:
        options["columns"] = {"missing": ["id"]}
        options["cursor_columns"] = {"missing": "updated_at"}

    with pytest.raises(ValueError, match=message):
        supabase_source(engine, **options)


def test_dlt_incremental_update_delete_and_empty_anchor(tmp_path):
    """Offline DLT run proves merge, tombstones, and the empty-corpus anchor."""
    import dlt

    source_engine = _database(tmp_path)
    source_table = Table("customers", MetaData(), autoload_with=source_engine)
    start = datetime(2026, 1, 1, tzinfo=UTC)

    destination_url = f"sqlite:///{tmp_path / 'destination.db'}"
    pipeline = dlt.pipeline(
        pipeline_name="supabase_connector_test",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.sqlalchemy(credentials=destination_url),
        dataset_name="supabase_test",
    )

    def run_sync():
        return pipeline.run(
            supabase_source(
                source_engine,
                project_ref="project",
                schema="main",
                tables=["customers"],
                columns={"customers": ["id", "name", "updated_at"]},
                cursor_columns={"customers": "updated_at"},
                enforce_read_only=False,
            ),
            write_disposition="merge",
        )

    def staged_rows():
        # The SQLAlchemy destination maps a SQLite dataset to a sibling file.
        destination_engine = create_engine(
            f"sqlite:///{tmp_path / 'destination__supabase_test.db'}"
        )
        table_names = inspect(destination_engine).get_table_names()
        target_name = next((name for name in table_names if name.endswith("supabase_rows")), None)
        if target_name is None:
            return []
        target = Table(target_name, MetaData(), autoload_with=destination_engine)
        with destination_engine.connect() as db:
            return [dict(row._mapping) for row in db.execute(select(target))]

    # An initially empty selected table is a successful, non-destructive sync.
    run_sync()
    assert staged_rows() == []

    with source_engine.begin() as db:
        db.execute(
            insert(source_table),
            [
                {"id": 1, "name": "Ada", "private_note": "x", "updated_at": start},
                {"id": 2, "name": "Grace", "private_note": "y", "updated_at": start},
            ],
        )
    run_sync()
    assert {row["id"] for row in staged_rows()} == {
        'supabase:project:main:customers:{"id":1}',
        'supabase:project:main:customers:{"id":2}',
    }

    # A second run has no cursor delta and leaves staging unchanged.
    no_change_info = run_sync()
    assert not no_change_info.loads_ids
    assert len(staged_rows()) == 2

    with source_engine.begin() as db:
        db.execute(
            update(source_table)
            .where(source_table.c.id == 1)
            .values(name="Ada Lovelace", updated_at=start + timedelta(seconds=1))
        )
        db.execute(delete(source_table).where(source_table.c.id == 2))
    run_sync()
    rows = staged_rows()
    assert len(rows) == 1
    assert "Ada Lovelace" in rows[0]["content"]

    # A pure deletion emits a tombstone even though the cursor resource has no rows.
    # The metadata-only anchor keeps cognee's reconciliation set non-empty.
    with source_engine.begin() as db:
        db.execute(delete(source_table))
    run_sync()
    rows = staged_rows()
    assert len(rows) == 1
    assert rows[0]["id"].endswith(":__sync_anchor__")
    assert '"record_count":0' in rows[0]["content"]

    # Once data returns, its higher cursor is ingested and the anchor is removed.
    with source_engine.begin() as db:
        db.execute(
            insert(source_table).values(
                id=3,
                name="Katherine",
                private_note="z",
                updated_at=start + timedelta(seconds=2),
            )
        )
    run_sync()
    rows = staged_rows()
    assert len(rows) == 1
    assert rows[0]["id"] == 'supabase:project:main:customers:{"id":3}'


def test_dlt_incremental_cursor_is_isolated_by_project_ref(tmp_path):
    """A new project must not inherit another project's incremental cursor."""
    import dlt

    newer_project = _database(tmp_path, "newer-project.db")
    older_project = _database(tmp_path, "older-project.db")
    newer_table = Table("customers", MetaData(), autoload_with=newer_project)
    older_table = Table("customers", MetaData(), autoload_with=older_project)

    with newer_project.begin() as db:
        db.execute(
            insert(newer_table).values(
                id=1,
                name="Newer project row",
                private_note="x",
                updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
        )
    with older_project.begin() as db:
        db.execute(
            insert(older_table).values(
                id=1,
                name="Older project row",
                private_note="y",
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

    pipeline = dlt.pipeline(
        pipeline_name="supabase_project_isolation_test",
        pipelines_dir=str(tmp_path / "project-isolation-pipelines"),
        destination=dlt.destinations.sqlalchemy(
            credentials=f"sqlite:///{tmp_path / 'project-isolation-destination.db'}"
        ),
        dataset_name="supabase_project_isolation",
    )

    def run_sync(engine, project_ref):
        return pipeline.run(
            supabase_source(
                engine,
                project_ref=project_ref,
                schema="main",
                tables=["customers"],
                columns={"customers": ["id", "name", "updated_at"]},
                cursor_columns={"customers": "updated_at"},
                enforce_read_only=False,
            ),
            write_disposition="merge",
        )

    run_sync(newer_project, "newer-project")
    run_sync(older_project, "older-project")

    destination_engine = create_engine(
        f"sqlite:///{tmp_path / 'project-isolation-destination__supabase_project_isolation.db'}"
    )
    target_name = next(
        name
        for name in inspect(destination_engine).get_table_names()
        if name.endswith("supabase_rows")
    )
    target = Table(target_name, MetaData(), autoload_with=destination_engine)
    with destination_engine.connect() as db:
        staged_ids = {row.id for row in db.execute(select(target.c.id))}

    assert 'supabase:older-project:main:customers:{"id":1}' in staged_ids, (
        "The first sync for a different project reused the previous project's cursor"
    )
