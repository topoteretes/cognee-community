"""The sync state machine against a real SQL engine (see fake_redshift.py).

Covers the ingest path (rows, queries, table cards), the timestamp cursor
(ties, late loads, full refresh, changed selections) and forget-on-delete
(deleted rows, soft deletes, dropped tables, removed selections), plus the
rule that a failed statement changes nothing.
"""

import json

import pytest
from fake_redshift import FakeRedshift

from cognee_community_connector_redshift import RedshiftQuery, RedshiftTable
from cognee_community_connector_redshift.redshift import (
    _normalize_selections,
    sync_rows,
)

TICKETS = {"id": "INTEGER", "subject": "VARCHAR(256)", "status": "VARCHAR(32)"}
TICKETS_WITH_CURSOR = {**TICKETS, "updated_at": "TIMESTAMP"}


class Run:
    """The rows, tombstones and stats of one sync."""

    def __init__(self, emitted, stats):
        self.rows: dict[str, dict] = {}
        self.deleted: set[str] = set()
        self.tables: dict[str, str] = {}
        for table, row in emitted:
            if row["_deleted"]:
                self.deleted.add(row["id"])
            else:
                self.rows[row["id"]] = row
            self.tables[row["id"]] = table
        self.stats = stats

    @property
    def row_ids(self) -> set[str]:
        return {row_id for row_id in self.rows if ":table:" not in row_id}


def sync(fake, selections, state, **kwargs) -> Run:
    connection = fake.connection()
    stats: dict = {}
    try:
        emitted = list(
            sync_rows(
                connection,
                _normalize_selections(selections, "redshift"),
                state,
                stats=stats,
                **kwargs,
            )
        )
    finally:
        connection.close()
    return Run(emitted, stats)


@pytest.fixture
def fake(tmp_path):
    return FakeRedshift(tmp_path)


@pytest.fixture
def tickets(fake):
    fake.create_table(
        "tickets",
        TICKETS_WITH_CURSOR,
        comment="Support tickets from the help desk.",
        column_comments={"status": "open, pending or solved"},
    )
    fake.insert(
        "tickets",
        {"id": 1, "subject": "Login fails", "status": "open", "updated_at": "2026-01-01 10:00:00"},
        {"id": 2, "subject": "Refund", "status": "solved", "updated_at": "2026-01-02 10:00:00"},
    )
    return RedshiftTable("public.tickets", primary_key="id", cursor_column="updated_at")


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
def test_first_sync_renders_rows_and_a_table_card(fake, tickets):
    state: dict = {}
    run = sync(fake, [tickets], state)

    assert run.row_ids == {"redshift:public.tickets:1", "redshift:public.tickets:2"}
    row = run.rows["redshift:public.tickets:1"]
    assert row["title"] == "public.tickets 1"
    assert row["content"].splitlines() == [
        "Row of Redshift table public.tickets (id = 1)",
        "",
        "id: 1",
        "subject: Login fails",
        "status: open",
        "updated_at: 2026-01-01 10:00:00",
    ]
    assert row["cognee_node_set"] == ["table:public.tickets"]
    assert run.tables["redshift:public.tickets:1"] == "redshift_public_tickets"

    card = run.rows["redshift:table:public.tickets"]
    assert run.tables["redshift:table:public.tickets"] == "redshift_table_cards"
    assert card["cognee_node_set"] == ["table:public.tickets"]
    assert "Description: Support tickets from the help desk." in card["content"]
    assert "- status (VARCHAR(32)): open, pending or solved" in card["content"]
    assert "Updated-at column: updated_at" in card["content"]

    assert state["selections"]["public.tickets"]["cursor"] == "2026-01-02 10:00:00"
    assert state["selections"]["public.tickets"]["keys"] == ["1", "2"]
    assert run.stats["rows"] == 2


def test_columns_limit_what_is_ingested_but_keep_key_and_cursor(fake, tickets):
    narrow = RedshiftTable(
        "tickets", primary_key="id", cursor_column="updated_at", columns=["subject"]
    )
    run = sync(fake, [narrow], {})

    content = run.rows["redshift:public.tickets:1"]["content"]
    assert "subject: Login fails" in content
    assert "id: 1" in content
    assert "updated_at: 2026-01-01 10:00:00" in content
    assert "status" not in content


def test_null_values_are_left_out_and_null_keys_are_skipped(fake):
    fake.create_table("notes", {"id": "INTEGER", "body": "TEXT", "owner": "TEXT"})
    fake.insert("notes", {"id": 1, "body": "hello", "owner": None}, {"id": None, "body": "lost"})

    run = sync(fake, [RedshiftTable("notes", primary_key="id")], {})

    assert run.row_ids == {"redshift:public.notes:1"}
    assert "owner" not in run.rows["redshift:public.notes:1"]["content"]
    assert run.stats["skipped"] == 1


def test_duplicate_keys_keep_one_row(fake):
    fake.create_table("events", {"id": "INTEGER", "name": "TEXT"})
    fake.insert("events", {"id": 1, "name": "first"}, {"id": 1, "name": "second"})

    run = sync(fake, [RedshiftTable("events", primary_key="id")], {})

    # One row per id reaches dlt, so its merge never picks between two.
    assert run.row_ids == {"redshift:public.events:1"}
    assert "name: first" in run.rows["redshift:public.events:1"]["content"]
    assert run.stats == {**run.stats, "rows": 1, "duplicates": 1}


def test_composite_primary_key(fake):
    fake.create_table("lines", {"order_id": "INTEGER", "line": "INTEGER", "sku": "TEXT"})
    fake.insert("lines", {"order_id": 7, "line": 1, "sku": "A"}, {"order_id": 7, "line": 2})

    state: dict = {}
    run = sync(fake, [RedshiftTable("lines", primary_key=["order_id", "line"])], state)

    row_id = 'redshift:public.lines:["7", "1"]'
    assert row_id in run.rows
    assert "(order_id = 7, line = 1)" in run.rows[row_id]["content"]

    fake.delete("lines", "line", 2)
    run = sync(fake, [RedshiftTable("lines", primary_key=["order_id", "line"])], state)
    assert run.deleted == {'redshift:public.lines:["7", "2"]'}


def test_query_selection(fake, tickets):
    query = RedshiftQuery(
        name="ticket_counts",
        sql="SELECT status, COUNT(*) AS tickets FROM public.tickets GROUP BY status;",
        primary_key="status",
    )
    run = sync(fake, [query], {})

    assert run.row_ids == {"redshift:ticket_counts:open", "redshift:ticket_counts:solved"}
    row = run.rows["redshift:ticket_counts:open"]
    assert row["content"].startswith("Row of Redshift query ticket_counts (status = open)")
    assert "tickets: 1" in row["content"]
    assert row["cognee_node_set"] == ["query:ticket_counts"]
    assert run.tables["redshift:ticket_counts:open"] == "redshift_query_ticket_counts"
    # Queries have no catalog entry, so no card and no catalog lookup.
    assert "redshift:table:ticket_counts" not in run.rows
    assert not any("svv_all" in sql for sql, _ in fake.statements)


def test_values_are_passed_as_parameters_never_formatted_into_sql(fake):
    fake.create_table("people", {"name": "TEXT", "updated_at": "TIMESTAMP"})
    fake.insert("people", {"name": "O'Brien", "updated_at": "2026-01-01 00:00:00"})
    selection = RedshiftTable("people", primary_key="name", cursor_column="updated_at")
    state: dict = {}
    sync(fake, [selection], state)

    # A late row is fetched by key, with the key bound as a parameter.
    fake.insert("people", {"name": "D'Arcy", "updated_at": "2025-01-01 00:00:00"})
    run = sync(fake, [selection], state)

    assert "redshift:public.people:D'Arcy" in run.rows
    assert all("D'Arcy" not in sql and "O'Brien" not in sql for sql, _ in fake.statements)


# ---------------------------------------------------------------------------
# Incremental cursor
# ---------------------------------------------------------------------------
def test_second_run_reads_only_rows_from_the_cursor_on(fake, tickets):
    state: dict = {}
    sync(fake, [tickets], state)
    fake.statements.clear()

    run = sync(fake, [tickets], state)

    # Only the row at the cursor value is re-read (>=); row 1 is not.
    assert run.row_ids == {"redshift:public.tickets:2"}
    rows_sql, params = next((s, p) for s, p in fake.statements if ">= :cognee_cursor" in s)
    assert params == {"cognee_cursor": "2026-01-02 10:00:00"}
    assert 'ORDER BY "updated_at" ASC NULLS FIRST' in rows_sql


def test_edit_moves_the_cursor_and_is_reingested(fake, tickets):
    state: dict = {}
    sync(fake, [tickets], state)

    fake.update("tickets", "id", 1, status="solved", updated_at="2026-01-03 09:00:00")
    run = sync(fake, [tickets], state)

    assert "status: solved" in run.rows["redshift:public.tickets:1"]["content"]
    assert state["selections"]["public.tickets"]["cursor"] == "2026-01-03 09:00:00"


def test_rows_sharing_the_cursor_value_are_not_skipped(fake, tickets):
    state: dict = {}
    sync(fake, [tickets], state)

    # Committed after the last run, with the same timestamp as the cursor.
    fake.insert(
        "tickets",
        {"id": 3, "subject": "Same second", "status": "open", "updated_at": "2026-01-02 10:00:00"},
    )
    run = sync(fake, [tickets], state)

    assert "redshift:public.tickets:3" in run.rows
    # Read by the cursor query itself, not rescued by the key inventory.
    assert run.stats["late"] == 0


def test_late_row_below_the_cursor_is_fetched_by_key(fake, tickets):
    state: dict = {}
    sync(fake, [tickets], state)

    # A backfill loads a row with an old timestamp.
    fake.insert(
        "tickets",
        {"id": 9, "subject": "Backfilled", "status": "open", "updated_at": "2025-06-01 00:00:00"},
    )
    run = sync(fake, [tickets], state)

    assert "redshift:public.tickets:9" in run.rows
    assert run.stats["late"] == 1
    # The cursor does not move back.
    assert state["selections"]["public.tickets"]["cursor"] == "2026-01-02 10:00:00"
    assert "9" in state["selections"]["public.tickets"]["keys"]


def test_unchanged_rows_below_the_cursor_are_not_reread(fake, tickets):
    state: dict = {}
    sync(fake, [tickets], state)
    run = sync(fake, [tickets], state)

    assert "redshift:public.tickets:1" not in run.rows
    assert run.stats["late"] == 0


def test_full_refresh_reads_everything(fake, tickets):
    state: dict = {}
    sync(fake, [tickets], state)
    # An update that does not touch the cursor column.
    fake.update("tickets", "id", 1, subject="Login fails on Safari")

    assert "redshift:public.tickets:1" not in sync(fake, [tickets], state).rows
    run = sync(fake, [tickets], state, full_refresh=True)
    assert "Safari" in run.rows["redshift:public.tickets:1"]["content"]


def test_changed_selection_reads_everything_again(fake, tickets):
    state: dict = {}
    sync(fake, [tickets], state)

    narrower = RedshiftTable(
        "public.tickets", primary_key="id", cursor_column="updated_at", columns=["subject"]
    )
    run = sync(fake, [narrower], state)

    assert run.row_ids == {"redshift:public.tickets:1", "redshift:public.tickets:2"}
    assert "status" not in run.rows["redshift:public.tickets:1"]["content"]


def test_table_without_cursor_is_read_whole_every_run(fake):
    fake.create_table("plans", {"id": "INTEGER", "name": "TEXT"})
    fake.insert("plans", {"id": 1, "name": "Basic"})
    state: dict = {}
    sync(fake, [RedshiftTable("plans", primary_key="id")], state)

    run = sync(fake, [RedshiftTable("plans", primary_key="id")], state)

    assert run.row_ids == {"redshift:public.plans:1"}
    assert state["selections"]["public.plans"]["cursor"] is None


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------
def test_deleted_row_is_tombstoned_in_its_staging_table(fake, tickets):
    state: dict = {}
    sync(fake, [tickets], state)

    fake.delete("tickets", "id", 1)
    run = sync(fake, [tickets], state)

    assert run.deleted == {"redshift:public.tickets:1"}
    assert run.tables["redshift:public.tickets:1"] == "redshift_public_tickets"
    assert state["selections"]["public.tickets"]["keys"] == ["2"]


def test_deleting_the_last_row_forgets_it(fake, tickets):
    state: dict = {}
    sync(fake, [tickets], state)
    fake.delete("tickets", "id", 1)
    fake.delete("tickets", "id", 2)

    run = sync(fake, [tickets], state)

    assert run.deleted == {"redshift:public.tickets:1", "redshift:public.tickets:2"}
    assert state["selections"]["public.tickets"]["keys"] == []


def test_deleted_row_is_forgotten_without_a_cursor_too(fake):
    fake.create_table("plans", {"id": "INTEGER", "name": "TEXT"})
    fake.insert("plans", {"id": 1, "name": "Basic"}, {"id": 2, "name": "Pro"})
    state: dict = {}
    sync(fake, [RedshiftTable("plans", primary_key="id")], state)

    fake.delete("plans", "id", 2)
    run = sync(fake, [RedshiftTable("plans", primary_key="id")], state)

    assert run.deleted == {"redshift:public.plans:2"}


def test_soft_deleted_row_leaves_the_where_filter_and_is_forgotten(fake):
    fake.create_table(
        "accounts",
        {"id": "INTEGER", "name": "TEXT", "deleted_at": "TIMESTAMP", "updated_at": "TIMESTAMP"},
    )
    fake.insert(
        "accounts",
        {"id": 1, "name": "Acme", "updated_at": "2026-01-01 00:00:00"},
        {"id": 2, "name": "Globex", "updated_at": "2026-01-01 00:00:00"},
    )
    selection = RedshiftTable(
        "accounts", primary_key="id", cursor_column="updated_at", where="deleted_at IS NULL"
    )
    state: dict = {}
    sync(fake, [selection], state)

    fake.update("accounts", "id", 2, deleted_at="2026-02-01 00:00:00", updated_at="2026-02-01")
    run = sync(fake, [selection], state)

    assert run.deleted == {"redshift:public.accounts:2"}
    assert "redshift:public.accounts:2" not in run.rows


def test_dropped_table_is_forgotten_with_its_card(fake, tickets):
    state: dict = {}
    sync(fake, [tickets], state)

    fake.drop_table("tickets")
    run = sync(fake, [tickets], state)

    assert run.deleted == {
        "redshift:public.tickets:1",
        "redshift:public.tickets:2",
        "redshift:table:public.tickets",
    }
    assert state["selections"] == {}
    assert state["cards"] == []


def test_table_missing_from_the_catalog_but_readable_is_kept(fake, tickets):
    state: dict = {}
    sync(fake, [tickets], state)

    # The catalog view stops listing it (a privilege change, a view glitch), yet
    # Redshift still answers SELECTs on it: that is not a drop.
    fake.hide_from_catalog("tickets")
    run = sync(fake, [tickets], state)

    assert run.deleted == set()
    assert "redshift:public.tickets:2" in run.rows
    assert state["cards"] == ["public.tickets"]
    assert state["selections"]["public.tickets"]["keys"] == ["1", "2"]


@pytest.mark.parametrize("answer", ["ERROR: permission denied for relation tickets", "timeout"])
def test_a_table_that_cannot_be_confirmed_gone_is_never_forgotten(fake, tickets, answer):
    state: dict = {}
    sync(fake, [tickets], state)
    before = json.dumps(state, sort_keys=True)

    fake.hide_from_catalog("tickets")
    fake.fail_on, fake.fail_message = "LIMIT 0", answer
    with pytest.raises(RuntimeError, match=answer.split(":")[-1].strip()[:10]):
        sync(fake, [tickets], state)

    assert json.dumps(state, sort_keys=True) == before


def test_unknown_table_on_first_sync_is_an_error_not_a_silent_skip(fake):
    with pytest.raises(ValueError, match="was not found"):
        sync(fake, [RedshiftTable("public.tickts", primary_key="id")], {})


def test_removed_selection_is_forgotten(fake, tickets):
    fake.create_table("plans", {"id": "INTEGER", "name": "TEXT"})
    fake.insert("plans", {"id": 1, "name": "Basic"})
    plans = RedshiftTable("plans", primary_key="id")
    state: dict = {}
    sync(fake, [tickets, plans], state)

    run = sync(fake, [plans], state)

    assert run.deleted == {
        "redshift:public.tickets:1",
        "redshift:public.tickets:2",
        "redshift:table:public.tickets",
    }
    assert run.tables["redshift:public.tickets:1"] == "redshift_public_tickets"
    assert set(state["selections"]) == {"public.plans"}


def test_turning_off_table_cards_forgets_them(fake, tickets):
    state: dict = {}
    sync(fake, [tickets], state)

    run = sync(fake, [tickets], state, include_table_cards=False)

    assert run.deleted == {"redshift:table:public.tickets"}


def test_comment_change_updates_the_card(fake, tickets):
    state: dict = {}
    sync(fake, [tickets], state)

    fake.comment_on("tickets", "Tickets from the help desk and email.")
    run = sync(fake, [tickets], state)

    assert "help desk and email" in run.rows["redshift:table:public.tickets"]["content"]


# ---------------------------------------------------------------------------
# Errors never delete
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("failing", ["svv_all_tables", ">= :cognee_cursor", 'SELECT CAST("id"'])
def test_failed_statement_changes_nothing(fake, tickets, failing):
    state: dict = {}
    sync(fake, [tickets], state)
    before = json.dumps(state, sort_keys=True)
    fake.delete("tickets", "id", 1)

    fake.fail_on = failing
    with pytest.raises(RuntimeError, match="statement failed"):
        sync(fake, [tickets], state)

    assert json.dumps(state, sort_keys=True) == before


# ---------------------------------------------------------------------------
# Selection validation
# ---------------------------------------------------------------------------
def test_selection_validation():
    with pytest.raises(ValueError, match="at least one"):
        _normalize_selections([], "redshift")
    with pytest.raises(ValueError, match="unique"):
        _normalize_selections(
            [RedshiftTable("a", primary_key="id"), RedshiftTable("public.a", primary_key="id")],
            "redshift",
        )
    with pytest.raises(ValueError, match="unique"):
        # Different labels, same staging table name.
        _normalize_selections(
            [RedshiftTable("a.b_c", primary_key="id"), RedshiftTable("a_b.c", primary_key="id")],
            "redshift",
        )
    with pytest.raises(ValueError, match="primary_key"):
        _normalize_selections([RedshiftTable("a", primary_key=[])], "redshift")
    with pytest.raises(ValueError, match=r"schema\.table"):
        _normalize_selections([RedshiftTable("db.schema.table", primary_key="id")], "redshift")
    with pytest.raises(ValueError, match="name and sql"):
        _normalize_selections([RedshiftQuery("q", sql=" ;", primary_key="id")], "redshift")


def test_identifiers_are_quoted():
    (selection,) = _normalize_selections(
        [RedshiftTable('odd"schema.my table', primary_key="id")], "redshift"
    )
    assert selection.relation == '"odd""schema"."my table"'
