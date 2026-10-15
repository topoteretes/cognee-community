"""Real dlt extraction/merge/state tests; SQLite is only the upstream fixture."""

from copy import deepcopy
from datetime import datetime, timedelta

import dlt
import pytest
from sqlalchemy import (
    MetaData,
    Table,
    create_engine,
    delete,
    event,
    insert,
    inspect,
    select,
    update,
)
from test_supabase import _database

from cognee_community_connector_supabase import supabase_source


class Sync:
    def __init__(self, tmp_path, dataset="test", pipeline_name="shared"):
        self.path = tmp_path
        self.dataset = dataset
        self.pipeline_name = pipeline_name
        self.restore()

    def restore(self):
        self.pipeline = dlt.pipeline(
            pipeline_name=self.pipeline_name,
            pipelines_dir=str(self.path / "pipelines"),
            destination=dlt.destinations.sqlalchemy(
                credentials=f"sqlite:///{self.path / 'stage.db'}"
            ),
            dataset_name=self.dataset,
        )

    def source(self, engine, project="a", name="supabase"):
        return supabase_source(
            engine,
            project_ref=project,
            source_name=name,
            schema="main",
            tables=["customers"],
            columns={"customers": ["id", "name"]},
            cursor_columns={"customers": "updated_at"},
            enforce_read_only=False,
            chunk_size=1,
        )

    def run(self, engine, project="a", name="supabase"):
        return self.pipeline.run(self.source(engine, project, name), write_disposition="merge")

    def rows(self):
        engine = create_engine(f"sqlite:///{self.path / f'stage__{self.dataset}.db'}")
        try:
            result = {}
            for name in inspect(engine).get_table_names():
                if name.endswith("supabase_rows"):
                    table = Table(name, MetaData(), autoload_with=engine)
                    with engine.connect() as db:
                        result[name] = {row.id: row.content for row in db.execute(select(table))}
            return result
        finally:
            engine.dispose()


def seed(engine, name, ids=(1, 2)):
    table = Table("customers", MetaData(), autoload_with=engine)
    with engine.begin() as db:
        db.execute(
            insert(table),
            [{"id": key, "name": name, "updated_at": datetime(2026, 1, 1)} for key in ids],
        )
    return table


@pytest.mark.parametrize("other_scope", ["project", "source"])
def test_alternating_scopes_update_delete_empty_restore(tmp_path, other_scope):
    a, b = _database(tmp_path, "a.db"), _database(tmp_path, "b.db")
    seed(a, "A preserved")
    table = seed(b, "B initial")
    sync = Sync(tmp_path)
    project, name = ("b", "supabase") if other_scope == "project" else ("a", "other")
    sync.run(a)
    preserved = deepcopy(sync.rows())

    def check_a():
        rows = sync.rows()
        for name, contents in preserved.items():
            assert rows[name] == contents

    sync.run(b, project, name)
    check_a()
    sync.restore()
    sync.run(b, project, name)
    check_a()
    with b.begin() as db:
        db.execute(
            update(table)
            .where(table.c.id == 1)
            .values(name="B updated", updated_at=datetime(2026, 1, 2))
        )
        db.execute(delete(table).where(table.c.id == 2))
    sync.run(b, project, name)
    check_a()
    b_rows = [contents for key, contents in sync.rows().items() if key not in preserved]
    assert len(b_rows) == 1 and len(b_rows[0]) == 1
    assert "B updated" in next(iter(b_rows[0].values()))
    with b.begin() as db:
        db.execute(delete(table))
    sync.run(b, project, name)
    sync.restore()
    sync.run(b, project, name)
    check_a()
    b_rows = [contents for key, contents in sync.rows().items() if key not in preserved]
    assert b_rows == [{}]


def test_equal_timestamp_insert_and_update(tmp_path):
    engine = _database(tmp_path)
    table = seed(engine, "initial", ids=(1,))
    sync = Sync(tmp_path)
    sync.run(engine)
    with engine.begin() as db:
        db.execute(
            insert(table).values(id=2, name="same boundary", updated_at=datetime(2026, 1, 1))
        )
    sync.run(engine)
    assert len(next(iter(sync.rows().values()))) == 2
    # Boundary rows must be replayed, including an update and a reversion at the same timestamp.
    for name in ("changed", "initial"):
        with engine.begin() as db:
            db.execute(update(table).where(table.c.id == 1).values(name=name))
        sync.run(engine)
        rows = next(iter(sync.rows().values()))
        assert f'"name":"{name}"' in rows['supabase:a:main:customers:{"id":1}']
        sync.run(engine)
        assert sync.rows() == {next(iter(sync.rows())): rows}


def test_selection_a_b_a_backfills_and_removes_deselected_columns(tmp_path):
    engine = _database(tmp_path)
    table = seed(engine, "older than watermark")
    with engine.begin() as db:
        db.execute(update(table).values(private_note="synthetic-private"))
        db.execute(update(table).where(table.c.id == 2).values(updated_at=datetime(2026, 1, 2)))
    sync = Sync(tmp_path)

    def run(columns):
        sync.restore()
        sync.pipeline.run(
            supabase_source(
                engine,
                project_ref="a",
                schema="main",
                tables=["customers"],
                columns={"customers": columns},
                cursor_columns={"customers": "updated_at"},
                enforce_read_only=False,
            ),
            write_disposition="merge",
        )
        return next(iter(sync.rows().values()))

    original = run(["id", "name"])
    expanded = run(["id", "name", "private_note"])
    assert len(expanded) == 2
    assert all("synthetic-private" in content for content in expanded.values())
    restored = run(["id", "name"])
    assert restored == original  # Includes id=1, strictly below the old A watermark.
    assert all("private_note" not in content for content in restored.values())
    assert run(["id", "name"]) == original


def test_deselected_table_reselection_backfills_historical_rows(tmp_path):
    engine = _database(tmp_path)
    customers = seed(engine, "customers")
    metadata = MetaData()
    orders = customers.to_metadata(metadata, name="orders")
    metadata.create_all(engine)
    with engine.begin() as db:
        db.execute(
            insert(orders),
            [
                {"id": 1, "name": "old order", "updated_at": datetime(2026, 1, 1)},
                {"id": 2, "name": "new order", "updated_at": datetime(2026, 1, 2)},
            ],
        )
    sync = Sync(tmp_path)

    def run(tables):
        sync.restore()
        sync.pipeline.run(
            supabase_source(
                engine,
                project_ref="a",
                schema="main",
                tables=tables,
                columns={name: ["id", "name"] for name in tables},
                cursor_columns=dict.fromkeys(tables, "updated_at"),
                enforce_read_only=False,
            ),
            write_disposition="merge",
        )
        return next(iter(sync.rows().values()))

    original = run(["customers", "orders"])
    assert len(original) == 4
    customers_only = run(["customers"])
    assert len(customers_only) == 2
    assert all(":orders:" not in key for key in customers_only)
    restored = run(["customers", "orders"])
    assert restored == original  # The removed old order must be backfilled, not just id=2.
    assert run(["customers", "orders"]) == original


@pytest.mark.parametrize("failure", ["rows", "sweep"])
def test_failed_extraction_keeps_state_and_staging_then_retries(tmp_path, monkeypatch, failure):
    import cognee_community_connector_supabase.supabase as module

    engine = _database(tmp_path)
    table = seed(engine, "before")
    sync = Sync(tmp_path)
    sync.run(engine)
    rows_before = sync.rows()
    state_before = deepcopy(sync.pipeline.state["sources"])
    with engine.begin() as db:
        db.execute(delete(table).where(table.c.id == 2))
        db.execute(update(table).values(name="after", updated_at=datetime(2026, 1, 2)))
    source = sync.source(engine)
    original = module._document_row

    def fail_row(*args, **kwargs):
        raise RuntimeError("injected interrupted row extraction")

    def fail_scan(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT main.customers.id "):
            raise RuntimeError("injected incomplete key scan")

    if failure == "rows":
        monkeypatch.setattr(module, "_document_row", fail_row)
    else:
        event.listen(engine, "before_cursor_execute", fail_scan)
    try:
        with pytest.raises(Exception, match="injected"):
            sync.pipeline.run(source, write_disposition="merge")
    finally:
        monkeypatch.setattr(module, "_document_row", original)
        if failure == "sweep":
            event.remove(engine, "before_cursor_execute", fail_scan)
    assert sync.rows() == rows_before
    assert sync.pipeline.state["sources"] == state_before
    sync.restore()
    sync.run(engine)
    rows = next(iter(sync.rows().values()))
    assert len(rows) == 1 and "after" in next(iter(rows.values()))
    sync.run(engine)
    assert next(iter(sync.rows().values())) == rows


def test_legacy_state_is_preserved_and_not_adopted(tmp_path):
    engine = _database(tmp_path)
    seed(engine, "new scope", ids=(1,))
    sync = Sync(tmp_path)
    legacy_id = 'supabase:foreign:main:customers:{"id":1}'
    sync.pipeline.run(
        dlt.resource(
            [{"id": legacy_id, "content": "legacy preserved", "title": "legacy"}],
            name="supabase_rows",
            primary_key="id",
            write_disposition="merge",
        )
    )
    legacy = {
        "resources": {
            "supabase_deletion_sweep": {
                "known_ids": {"customers": ['supabase:foreign:main:customers:{"id":1}']},
                "anchor_active": True,
            }
        }
    }
    with sync.pipeline.managed_state() as state:
        state.setdefault("sources", {})["supabase"] = deepcopy(legacy)
    sync.run(engine)
    assert sync.pipeline.state["sources"]["supabase"] == legacy
    rows = sync.rows()
    assert rows["supabase_rows"] == {legacy_id: "legacy preserved"}
    assert len(rows) == 2
    assert all(len(contents) == 1 for contents in rows.values())


@pytest.mark.parametrize("bounded", [False, True])
def test_persisted_pre_v2_cursor_upgrade_backfills_without_resetting_scope(tmp_path, bounded):
    engine = _database(tmp_path)
    table = seed(engine, "before upgrade", ids=(1, 2, 3))
    with engine.begin() as db:
        db.execute(update(table).where(table.c.id == 2).values(updated_at=datetime(2026, 1, 2)))
    other = _database(tmp_path, "other.db")
    seed(other, "other project")
    sync = Sync(tmp_path)
    sync.run(other, project="other")
    preserved = deepcopy(sync.rows())
    other_scope = sync.source(other, project="other").name
    other_state = deepcopy(sync.pipeline.state["sources"][other_scope])
    sync.run(engine)
    scope = sync.source(engine).name

    # Persist the pre-v2 layout using real dlt-produced cursor values and staging.
    # Only the parent resource key changed at that upgrade boundary.
    with sync.pipeline.managed_state() as state:
        resources = state["sources"][scope]["resources"]
        cursor = next(name for name, value in resources.items() if "incremental" in value)
        assert cursor.endswith("_v2")
        old_cursor = cursor.removesuffix("_v2")
        resources[old_cursor] = resources.pop(cursor)
    sync.restore()
    resources = sync.pipeline.state["sources"][scope]["resources"]
    assert cursor not in resources
    assert resources[old_cursor]["incremental"]["updated_at"]["last_value"] == datetime(2026, 1, 2)
    baseline = deepcopy(resources[f"{scope}_deletion_sweep"]["known_ids"])
    assert len(baseline["customers"]) == 3
    before_tables = set(sync.rows())
    with engine.begin() as db:
        db.execute(update(table).where(table.c.id == 1).values(name="historical backfill"))
        db.execute(delete(table).where(table.c.id == 3))

    def upgraded_source():
        return supabase_source(
            engine,
            project_ref="a",
            schema="main",
            tables=["customers"],
            columns={"customers": ["id", "name"]},
            cursor_columns={"customers": "updated_at"},
            initial_values={"customers": datetime(2026, 1, 2)} if bounded else None,
            enforce_read_only=False,
        )

    sync.pipeline.run(upgraded_source(), write_disposition="merge")
    assert set(sync.rows()) == before_tables  # No new staging identity or duplicate documents.
    for name, rows in preserved.items():
        assert sync.rows()[name] == rows
    assert sync.pipeline.state["sources"][other_scope] == other_state
    rows = next(rows for name, rows in sync.rows().items() if name not in preserved)
    assert len(rows) == 2
    expected = "before upgrade" if bounded else "historical backfill"
    assert expected in rows['supabase:a:main:customers:{"id":1}']
    resources = sync.pipeline.state["sources"][scope]["resources"]
    assert "incremental" not in resources[old_cursor]
    assert "incremental" in resources[cursor]
    known_ids = resources[f"{scope}_deletion_sweep"]["known_ids"]["customers"]
    assert set(known_ids) == set(rows)
    after = deepcopy(sync.rows())
    sync.restore()
    sync.pipeline.run(upgraded_source(), write_disposition="merge")
    assert sync.rows() == after


def test_extraction_failure_after_cursor_cleanup_rolls_back_then_retries(tmp_path):
    engine = _database(tmp_path)
    table = seed(engine, "before")
    other = _database(tmp_path, "other.db")
    seed(other, "other scope")
    sync = Sync(tmp_path)
    sync.run(other, project="other")
    preserved = deepcopy(sync.rows())
    sync.run(engine)
    before_rows = deepcopy(sync.rows())
    before_state = deepcopy(sync.pipeline.state["sources"])
    scope = sync.source(engine).name
    inactive = next(
        name for name, value in before_state[scope]["resources"].items() if "incremental" in value
    )
    with engine.begin() as db:
        db.execute(delete(table).where(table.c.id == 2))
        db.execute(update(table).values(name="after", private_note="synthetic"))

    def changed_source():
        return supabase_source(
            engine,
            project_ref="a",
            schema="main",
            tables=["customers"],
            columns={"customers": ["id", "name", "private_note"]},
            cursor_columns={"customers": "updated_at"},
            enforce_read_only=False,
        )

    source = changed_source()
    sweep = source.resources[f"{scope}_deletion_sweep"]
    original = sweep._pipe.gen
    reached_cleanup = []

    def fail_after_cleanup():
        # Test-only hook: finish the real generator, including its state mutation,
        # before failing inside the same extraction transaction.
        yield from original()
        resources = dlt.current.source_state()["resources"]
        assert "incremental" not in resources[inactive]
        assert len(resources[f"{scope}_deletion_sweep"]["known_ids"]["customers"]) == 1
        reached_cleanup.append(True)
        raise RuntimeError("injected failure after cursor cleanup")

    sweep._pipe.replace_gen(fail_after_cleanup)
    with pytest.raises(Exception, match="injected failure after cursor cleanup"):
        sync.pipeline.run(source, write_disposition="merge")
    assert reached_cleanup == [True]
    assert sync.pipeline.state["sources"] == before_state
    assert sync.rows() == before_rows
    sync.restore()
    assert sync.pipeline.state["sources"] == before_state
    sync.pipeline.run(changed_source(), write_disposition="merge")
    for name, rows in preserved.items():
        assert sync.rows()[name] == rows
    other_scope = sync.source(other, project="other").name
    assert sync.pipeline.state["sources"][other_scope] == before_state[other_scope]
    rows = next(rows for name, rows in sync.rows().items() if name not in preserved)
    assert len(rows) == 1 and '"name":"after"' in next(iter(rows.values()))
    assert "synthetic" in next(iter(rows.values()))
    assert "incremental" not in sync.pipeline.state["sources"][scope]["resources"][inactive]
    after = deepcopy(sync.rows())
    sync.restore()
    sync.pipeline.run(changed_source(), write_disposition="merge")
    assert sync.rows() == after


def test_interrupted_load_retries_pending_package_without_duplicates(tmp_path, monkeypatch):
    engine = _database(tmp_path)
    table = seed(engine, "before", ids=(1,))
    sync = Sync(tmp_path)
    sync.run(engine)
    before = sync.rows()
    with engine.begin() as db:
        db.execute(update(table).values(name="after", updated_at=datetime(2026, 1, 2)))
    original_load = sync.pipeline.load

    def interrupted(*args, **kwargs):
        raise RuntimeError("injected load interruption")

    monkeypatch.setattr(sync.pipeline, "load", interrupted)
    with pytest.raises(Exception, match="injected load interruption"):
        sync.run(engine)
    assert sync.rows() == before
    monkeypatch.setattr(sync.pipeline, "load", original_load)
    sync.restore()
    sync.run(engine)
    sync.run(engine)
    rows = next(iter(sync.rows().values()))
    assert len(rows) == 1 and "after" in next(iter(rows.values()))


def test_dataset_pipeline_scope_and_restoration(tmp_path):
    # Exercise Cognee's real name derivation, not a copy in this test.
    from cognee.tasks.ingestion.dlt_utils import pipeline_name_for_source

    engine = _database(tmp_path)
    table = seed(engine, "both datasets", ids=(1,))
    source = Sync(tmp_path).source(engine)
    a = Sync(tmp_path, "dataset_a", pipeline_name_for_source(source, "dataset_a"))
    b = Sync(tmp_path, "dataset_b", pipeline_name_for_source(source, "dataset_b"))
    assert a.pipeline_name != b.pipeline_name
    a.run(engine)
    b.run(engine)
    before = a.rows()
    with engine.begin() as db:
        db.execute(
            update(table).values(name="B only", updated_at=datetime(2026, 1, 1) + timedelta(days=1))
        )
    b.run(engine)
    a.restore()
    assert a.rows() == before
    with engine.begin() as db:
        db.execute(delete(table))
    b.run(engine)
    b.restore()
    b.run(engine)
    assert all(not rows for rows in b.rows().values())
    assert a.rows() == before
