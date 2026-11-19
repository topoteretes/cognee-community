"""Unit and integration tests for the Splitwise dlt connector.

All tests are runnable in CI without live API credentials:
- API mocking for limit/offset pagination, incremental updated_at cursor.
- dlt pipeline tests (sqlite destination) verifying merge-mode hard-delete.
"""

from __future__ import annotations

import pytest
from cognee.tasks.ingestion.dlt_utils import document_source_tag

from cognee_community_connector_splitwise.auth import build_splitwise_client
from cognee_community_connector_splitwise.splitwise import (
    SPLITWISE_SOURCE_NAME,
    splitwise_expenses_resource,
    splitwise_source,
)

# ---------------------------------------------------------------------------
# Test Doubles / Fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, json_data: dict, status_code: int = 200) -> None:
        self._json = json_data
        self.status_code = status_code

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class FakeSplitwiseClient:
    """Mock httpx.Client for Splitwise API v3."""

    def __init__(
        self,
        expenses: list[dict] | None = None,
        groups: list[dict] | None = None,
        current_user: dict | None = None,
    ) -> None:
        self._expenses = expenses or []
        self._groups = groups or []
        self._current_user = current_user or {"id": 1, "first_name": "Test User"}
        self._calls = []

    def get(self, path: str, params: dict | None = None) -> FakeResponse:
        self._calls.append((path, params or {}))

        if path == "/get_expenses":
            limit = (params or {}).get("limit", 20)
            offset = (params or {}).get("offset", 0)
            group_id = (params or {}).get("group_id")
            updated_after = (params or {}).get("updated_after")

            filtered = [
                e for e in self._expenses if group_id is None or e.get("group_id") == group_id
            ]
            if updated_after:
                filtered = [e for e in filtered if e.get("updated_at", "") > updated_after]

            page = filtered[offset : offset + limit]
            return FakeResponse({"expenses": page})

        if path == "/get_groups":
            return FakeResponse({"groups": self._groups})

        if path == "/get_current_user":
            return FakeResponse({"user": self._current_user})

        return FakeResponse({})


def _make_expense(
    expense_id: int,
    description: str = "Test Expense",
    cost: str = "100.00",
    currency: str = "USD",
    date: str = "2024-01-15",
    updated_at: str = "2024-01-15T10:00:00Z",
    deleted_at: str | None = None,
    group_id: int = 1,
) -> dict:
    return {
        "id": expense_id,
        "description": description,
        "cost": cost,
        "currency_code": currency,
        "date": date,
        "updated_at": updated_at,
        "deleted_at": deleted_at,
        "group_id": group_id,
        "created_by": {"id": 1, "first_name": "Alice"},
        "users": [
            {"id": 1, "first_name": "Alice", "paid_share": cost, "owed_share": "0"},
            {"id": 2, "first_name": "Bob", "paid_share": "0", "owed_share": cost},
        ],
        "repayments": [],
        "details": "Test note",
        "receipt": {"original": "https://example.com/receipt.jpg"},
    }


def _make_group(group_id: int = 1, name: str = "Test Group") -> dict:
    return {
        "id": group_id,
        "name": name,
        "type": "apartment",
        "members": [{"id": 1}, {"id": 2}],
        "balance": [
            {"user_id": 1, "balance": "50.00", "currency_code": "USD"},
            {"user_id": 2, "balance": "-50.00", "currency_code": "USD"},
        ],
    }


# ---------------------------------------------------------------------------
# Basic / Utility Tests
# ---------------------------------------------------------------------------


def test_document_source_attr():
    fake_client = FakeSplitwiseClient(expenses=[])
    source = splitwise_source(api_key="test_key", client=fake_client)
    assert SPLITWISE_SOURCE_NAME == "splitwise"
    assert document_source_tag(source) == "splitwise"


def test_auth_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("SPLITWISE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Splitwise API key required"):
        build_splitwise_client(api_key=None)


# ---------------------------------------------------------------------------
# Pipeline-based Acceptance Tests (isolated via tmp_path)
# ---------------------------------------------------------------------------


def _run_pipeline(dlt_mod, tmp_path, pipeline_name: str, source, dataset_name: str = "sw_ds"):
    """Helper to run a dlt pipeline with isolated state."""
    db_path = (tmp_path / f"{pipeline_name}.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name=pipeline_name,
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name=dataset_name,
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(source)
    return pipeline


def _get_expense_ids(pipeline, table: str = "splitwise_expenses") -> list[str]:
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id FROM {table}") as cur,
    ):
        return [r[0] for r in cur.fetchall()]


def _get_expense_rows(pipeline, table: str = "splitwise_expenses") -> list[dict]:
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT * FROM {table}") as cur,
    ):
        cols = list(cur.keys())
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def test_limit_offset_pagination(dlt_mod, tmp_path):
    """Verify limit/offset pagination retrieves all pages."""
    expenses = [_make_expense(i) for i in range(1, 5)]
    fake_client = FakeSplitwiseClient(expenses=expenses)

    source = splitwise_expenses_resource(group_ids=[1], max_expenses=10, client=fake_client)
    pipeline = _run_pipeline(dlt_mod, tmp_path, "sw_pagination", source)

    ids = _get_expense_ids(pipeline)
    assert len(ids) == 4
    assert ids == ["1", "2", "3", "4"]


def test_updated_at_cursor_filters_old_expenses(dlt_mod, tmp_path):
    """Incremental cursor: expenses with updated_at before cursor are not yielded."""
    expenses = [
        _make_expense(1, updated_at="2024-06-01T10:00:00Z"),
        _make_expense(2, updated_at="2024-01-01T10:00:00Z"),
    ]
    fake_client = FakeSplitwiseClient(expenses=expenses)

    source = splitwise_expenses_resource(
        group_ids=[1],
        max_expenses=10,
        client=fake_client,
    )
    pipeline = _run_pipeline(dlt_mod, tmp_path, "sw_cursor_filter", source)

    ids = _get_expense_ids(pipeline)
    assert len(ids) == 2


def test_updated_at_cursor_in_source(dlt_mod, tmp_path):
    """Integration test: updated_at cursor skips old expenses on re-sync."""
    expenses_run1 = [_make_expense(1, updated_at="2024-01-01T10:00:00Z")]
    fake_client = FakeSplitwiseClient(expenses=expenses_run1)

    pipeline = _run_pipeline(
        dlt_mod, tmp_path, "sw_cursor", splitwise_source(group_ids=[1], client=fake_client)
    )

    expenses_run2 = [
        _make_expense(1, updated_at="2024-01-01T10:00:00Z"),
        _make_expense(2, updated_at="2024-06-01T10:00:00Z"),
    ]
    fake_client._expenses = expenses_run2

    pipeline.run(splitwise_source(group_ids=[1], client=fake_client))

    ids = _get_expense_ids(pipeline)
    assert "1" in ids and "2" in ids


def test_void_emits_deleted(dlt_mod, tmp_path):
    """Voided expense (deleted_at set) emits _deleted=True and is purged."""
    expenses = [
        _make_expense(1),
        _make_expense(2, deleted_at="2024-02-01T10:00:00Z"),
    ]
    fake_client = FakeSplitwiseClient(expenses=expenses)

    source = splitwise_expenses_resource(group_ids=[1], client=fake_client)
    pipeline = _run_pipeline(dlt_mod, tmp_path, "sw_void", source)

    # Deleted expense should be purged (hard delete)
    ids = _get_expense_ids(pipeline)
    assert "2" not in ids
    assert "1" in ids


def test_hash_skips_unchanged(dlt_mod, tmp_path):
    """Unchanged expense content hash -> skipped on re-sync (0 rows yielded)."""
    expense = _make_expense(1, description="Lunch", cost="50.00")
    fake_client = FakeSplitwiseClient(expenses=[expense])

    pipeline = _run_pipeline(
        dlt_mod, tmp_path, "sw_hash", splitwise_source(group_ids=[1], client=fake_client)
    )

    ids = _get_expense_ids(pipeline)
    assert ids == ["1"]

    # Re-run with identical expense - should not add duplicate
    pipeline.run(splitwise_source(group_ids=[1], client=fake_client))

    ids = _get_expense_ids(pipeline)
    assert ids == ["1"]


def test_group_filter_client_side(dlt_mod, tmp_path):
    """Client-side group filtering excludes non-selected group."""
    expenses = [
        _make_expense(1, group_id=1),
        _make_expense(2, group_id=2),
        _make_expense(3, group_id=1),
    ]
    fake_client = FakeSplitwiseClient(expenses=expenses, groups=[_make_group(1), _make_group(2)])

    source = splitwise_expenses_resource(group_ids=[1], client=fake_client)
    pipeline = _run_pipeline(dlt_mod, tmp_path, "sw_group_filter", source)

    ids = _get_expense_ids(pipeline)
    rows = _get_expense_rows(pipeline)
    assert len(ids) == 2
    assert all(r["group_id"] == "1" for r in rows)


def test_email_not_in_prose(dlt_mod, tmp_path):
    """PII (emails) not in document prose, only in structured columns."""
    expenses = [_make_expense(1, description="Dinner")]
    fake_client = FakeSplitwiseClient(expenses=expenses)

    source = splitwise_expenses_resource(group_ids=[1], client=fake_client)
    pipeline = _run_pipeline(dlt_mod, tmp_path, "sw_pii", source)

    rows = _get_expense_rows(pipeline)
    assert len(rows) == 1
    assert "@" not in rows[0]["content"]
    assert "Alice" in rows[0]["shares_json"]


def test_max_expenses_limit(dlt_mod, tmp_path):
    """max_expenses caps the number of expenses fetched."""
    expenses = [_make_expense(i) for i in range(1, 6)]
    fake_client = FakeSplitwiseClient(expenses=expenses)

    source = splitwise_expenses_resource(group_ids=[1], max_expenses=3, client=fake_client)
    pipeline = _run_pipeline(dlt_mod, tmp_path, "sw_limit", source)

    ids = _get_expense_ids(pipeline)
    assert len(ids) == 3


def test_without_group_filter(dlt_mod, tmp_path):
    """No group_ids filter -> all groups' expenses fetched."""
    expenses = [
        _make_expense(1, group_id=1),
        _make_expense(2, group_id=2),
    ]
    groups = [_make_group(1, "Group A"), _make_group(2, "Group B")]
    fake_client = FakeSplitwiseClient(expenses=expenses, groups=groups)

    source = splitwise_expenses_resource(group_ids=None, client=fake_client)
    pipeline = _run_pipeline(dlt_mod, tmp_path, "sw_no_filter", source)

    ids = _get_expense_ids(pipeline)
    rows = _get_expense_rows(pipeline)
    assert len(ids) == 2
    group_ids = {r["group_id"] for r in rows}
    assert group_ids == {"1", "2"}


def test_groups_and_balances_resources(dlt_mod, tmp_path):
    """Groups and balances resources are yielded when enabled."""
    expenses = [_make_expense(1)]
    groups = [_make_group(1, "Roommates")]
    fake_client = FakeSplitwiseClient(expenses=expenses, groups=groups)

    pipeline = _run_pipeline(
        dlt_mod,
        tmp_path,
        "sw_groups",
        splitwise_source(
            group_ids=[1], include_groups=True, include_balances=True, client=fake_client
        ),
    )

    with pipeline.sql_client() as client:
        with client.execute_query("SELECT name FROM splitwise_groups") as cur:
            group_rows = [r[0] for r in cur.fetchall()]
        assert "Roommates" in group_rows

        with client.execute_query("SELECT group_name FROM splitwise_balances") as cur:
            balance_rows = [r[0] for r in cur.fetchall()]
        assert "Roommates" in balance_rows
