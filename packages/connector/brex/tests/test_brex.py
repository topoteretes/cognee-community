"""Unit tests for Brex connector (100% offline, mocked client and data).

Covers:
- Token validation and error handling
- Resource selection and `include` validation
- Amount normalization (cents, decimals, zero, and negative refunds/credits)
- Currency formatting for positive and negative values
- Client error mappings (401, 403, 429)
- Multi-page pagination traversal
- Structured relational expenses ingestion and ID namespacing
- Document-mode routing and Markdown formatting for memos
- Structured relational budgets ingestion with parent-child links
- Incremental high-watermark cursor advancement and known_ids accumulation
- Deletion reconciliation on full refresh (`full_refresh=True`)
- Empty-sweep guardrails (skipping tombstones on unexpected empty responses)
- Anti-corruption abort guard on failure
- Dual-path routing separation (verifying document vs relational source tagging)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, document_source_tag
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

    def document_source_tag(item):
        return getattr(item, DOCUMENT_SOURCE_ATTR, None)


from cognee_community_connector_brex.brex import (
    BREX_BUDGETS_TABLE,
    BREX_EXPENSES_TABLE,
    BREX_MEMOS_TABLE,
    _budget_to_row,
    _expense_to_memo_card,
    _expense_to_row,
    _format_currency,
    _normalize_amount,
    brex_memos_source,
    brex_source,
)
from cognee_community_connector_brex.client import (
    BrexAuthError,
    BrexClient,
    BrexPermissionError,
    BrexRateLimitError,
)
from tests.conftest import MockBrexClient


def test_missing_token_raises_value_error():
    """Verify that omitting token and having no BREX_API_TOKEN raises ValueError."""
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(ValueError, match="Brex API token is required"),
    ):
        brex_source(token=None)


def test_invalid_or_empty_include_raises_value_error():
    """Verify that empty or unrecognized include list raises ValueError."""
    with pytest.raises(ValueError, match="include must not be empty"):
        brex_source(token="tok", include=[])

    with pytest.raises(ValueError, match="Invalid resource"):
        brex_source(token="tok", include=["invoices", "expenses"])


def test_normalize_amount_and_formatting():
    """Verify numeric amount parsing and formatting for +/- cents, floats, zero."""
    # Positive cents integer representation
    val, curr = _normalize_amount({"amount": 1050, "currency": "USD"})
    assert val == 10.50
    assert curr == "USD"
    assert _format_currency(val, curr) == "$10.50 USD"

    # Negative cents integer (refund / credit)
    val_neg, curr_neg = _normalize_amount({"amount": -1500, "currency": "USD"})
    assert val_neg == -15.00
    assert curr_neg == "USD"
    assert _format_currency(val_neg, curr_neg) == "-$15.00 USD"

    # Zero amount
    val_zero, curr_zero = _normalize_amount({"amount": 0, "currency": "USD"})
    assert val_zero == 0.0
    assert _format_currency(val_zero, curr_zero) == "$0.00 USD"

    # Float amount (positive and negative)
    val_f, curr_f = _normalize_amount(45.25)
    assert val_f == 45.25
    assert curr_f == "USD"

    val_f_neg, _ = _normalize_amount(-32.10)
    assert val_f_neg == -32.10
    assert _format_currency(val_f_neg, "EUR") == "-$32.10 EUR"

    # Missing or null amount
    val_none, curr_none = _normalize_amount(None)
    assert val_none is None
    assert curr_none is None
    assert _format_currency(None, None) == "N/A USD"


def test_client_error_handling():
    """Verify HTTP status codes translate to specific Brex exceptions."""
    session_mock = MagicMock()

    # 401 Unauthorized
    resp_401 = MagicMock(status_code=401, text="Unauthorized")
    session_mock.request.return_value = resp_401
    client = BrexClient(token="bad_token", session=session_mock)
    with pytest.raises(BrexAuthError, match="HTTP 401"):
        client.fetch_expenses()

    # 403 Forbidden
    resp_403 = MagicMock(status_code=403, text="Forbidden")
    session_mock.request.return_value = resp_403
    with pytest.raises(BrexPermissionError, match="HTTP 403"):
        client.fetch_expenses()

    # 429 Rate Limit
    resp_429 = MagicMock(status_code=429, text="Rate Limited", headers={"Retry-After": "0"})
    session_mock.request.return_value = resp_429
    client.max_retries = 1
    with pytest.raises(BrexRateLimitError, match="HTTP 429"):
        client.fetch_expenses()


def test_client_pagination():
    """Verify client traverses multi-page cursor responses seamlessly."""
    session_mock = MagicMock()
    page_1 = {
        "items": [{"id": "exp_1", "amount": 100}],
        "next_cursor": "cur_page_2",
    }
    page_2 = {
        "items": [{"id": "exp_2", "amount": 200}],
        "next_cursor": None,
    }

    resp_1 = MagicMock(status_code=200)
    resp_1.json.return_value = page_1
    resp_2 = MagicMock(status_code=200)
    resp_2.json.return_value = page_2
    session_mock.request.side_effect = [resp_1, resp_2]

    client = BrexClient(token="test_tok", session=session_mock)
    items = list(client.fetch_all_expenses())

    assert len(items) == 2
    assert items[0]["id"] == "exp_1"
    assert items[1]["id"] == "exp_2"


def test_expense_row_transformation(mock_expenses_data: list[dict[str, Any]]):
    """Verify expense conversion into relational row with normalized foreign keys."""
    raw = mock_expenses_data[0]
    row = _expense_to_row(raw, id_prefix="brex")

    assert row["id"] == "brex:expense:exp_001"
    assert row["expense_id"] == "exp_001"
    assert row["amount"] == 3420.00
    assert row["currency"] == "USD"
    assert row["merchant_name"] == "Datadog Inc"
    assert row["user_name"] == "Jane Doe"
    assert row["budget_id"] == "brex:budget:bgt_cloud_q2"
    assert row["category"] == "Software & Cloud Services"
    assert row["status"] == "APPROVED"
    assert row["_deleted"] is False


def test_memo_card_document_mode(mock_expenses_data: list[dict[str, Any]]):
    """Verify memo transformation into Markdown document card including refund cases."""
    raw = mock_expenses_data[0]
    card = _expense_to_memo_card(raw, id_prefix="brex")

    assert card["id"] == "brex:memo:exp_001"
    assert "Datadog Inc" in card["title"]
    assert "$3,420.00 USD" in card["title"]
    assert "# Expense: Datadog Inc ($3,420.00 USD)" in card["content"]
    assert "Jane Doe" in card["content"]
    assert "Engineering" in card["content"]
    assert "Monthly infrastructure log indexing" in card["content"]
    assert "brex:budget:bgt_cloud_q2" in card["content"]
    assert card["_deleted"] is False

    # Negative / refund memo
    refund_raw = dict(raw, id="exp_refund", amount={"amount": -4500, "currency": "USD"})
    refund_card = _expense_to_memo_card(refund_raw, id_prefix="brex")
    assert "-$45.00 USD" in refund_card["title"]
    assert "-$45.00 USD" in refund_card["content"]


def test_budget_row_transformation(mock_budgets_data: list[dict[str, Any]]):
    """Verify budget conversion into relational row with parent budget hierarchy."""
    child_raw = mock_budgets_data[0]
    child_row = _budget_to_row(child_raw, id_prefix="brex")

    assert child_row["id"] == "brex:budget:bgt_cloud_q2"
    assert child_row["name"] == "Q2 Cloud & Infrastructure Budget"
    assert child_row["limit_amount"] == 50000.00
    assert child_row["spent_amount"] == 18420.00
    assert child_row["parent_budget_id"] == "brex:budget:bgt_parent_eng"
    assert child_row["_deleted"] is False


def test_brex_source_resource_selection_and_document_mode(mock_brex_client):
    """Verify default resources, document-mode isolation, and dedicated memos source."""
    # 1. Default brex_source: relational resources, NO source-level document tag
    default_src = brex_source(token="mock_tok", client=mock_brex_client)
    assert set(default_src.selected_resources.keys()) == {BREX_EXPENSES_TABLE, BREX_BUDGETS_TABLE}
    assert getattr(default_src, DOCUMENT_SOURCE_ATTR, None) is None

    # 2. Mixed source: expenses, memos, budgets. Source level is NOT marked as document mode!
    mixed_src = brex_source(
        token="mock_tok",
        include=["expenses", "memos", "budgets"],
        client=mock_brex_client,
    )
    assert BREX_EXPENSES_TABLE in mixed_src.selected_resources
    assert BREX_MEMOS_TABLE in mixed_src.selected_resources
    assert BREX_BUDGETS_TABLE in mixed_src.selected_resources
    # Crucial: source-level DOCUMENT_SOURCE_ATTR must be None so relational tables aren't misrouted!
    assert getattr(mixed_src, DOCUMENT_SOURCE_ATTR, None) is None
    # brex_memos resource itself has the tag
    assert getattr(mixed_src.selected_resources[BREX_MEMOS_TABLE], DOCUMENT_SOURCE_ATTR) == "brex"

    # 3. Dedicated brex_memos_source: pure document mode
    memos_src = brex_memos_source(token="mock_tok", client=mock_brex_client)
    assert set(memos_src.selected_resources.keys()) == {BREX_MEMOS_TABLE}
    assert getattr(memos_src, DOCUMENT_SOURCE_ATTR) == "brex"
    assert document_source_tag(memos_src) == "brex"


def test_incremental_sync_and_known_ids_accumulation(mock_brex_client, mock_expenses_data):
    """Verify incremental delta accumulates new record IDs into known_ids."""
    # Initial run: mock client returns exp_001 and exp_002
    source = brex_source(
        token="mock_tok",
        include=["expenses"],
        client=mock_brex_client,
    )
    expenses_resource = source.selected_resources[BREX_EXPENSES_TABLE]

    state = {}
    with patch("dlt.current.resource_state", return_value=state):
        rows = list(expenses_resource())

    assert len(rows) == 2
    assert state.get("last_posted_at") == "2026-04-15T18:00:00Z"
    assert state.get("known_ids") == ["exp_001", "exp_002"]

    # Second run: new expense exp_003 posted after last watermark
    new_expense = {
        "id": "exp_003",
        "amount": {"amount": 500, "currency": "USD"},
        "merchant": {"name": "Github"},
        "posted_at": "2026-04-20T10:00:00Z",
    }
    mock_brex_client.expenses = [new_expense]

    source2 = brex_source(
        token="mock_tok",
        include=["expenses"],
        client=mock_brex_client,
    )
    expenses_resource2 = source2.selected_resources[BREX_EXPENSES_TABLE]

    with patch("dlt.current.resource_state", return_value=state):
        rows2 = list(expenses_resource2())

    assert len(rows2) == 1
    assert rows2[0]["expense_id"] == "exp_003"
    # State known_ids must retain exp_001 and exp_002 and append exp_003!
    assert state["known_ids"] == ["exp_001", "exp_002", "exp_003"]
    assert state["last_posted_at"] == "2026-04-20T10:00:00Z"


def test_second_run_deletion_on_full_refresh(mock_brex_client, mock_expenses_data):
    """Verify full_refresh reconciles against all known_ids and emits tombstones."""
    # State knew about exp_001, exp_002, and exp_003
    state = {
        "last_posted_at": "2026-04-20T10:00:00Z",
        "known_ids": ["exp_001", "exp_002", "exp_003"],
    }

    # Upstream deleted exp_002; client only returns exp_001 and exp_003
    exp_003 = {
        "id": "exp_003",
        "amount": {"amount": 500, "currency": "USD"},
        "merchant": {"name": "Github"},
        "posted_at": "2026-04-20T10:00:00Z",
    }
    mock_brex_client.expenses = [mock_expenses_data[0], exp_003]

    source = brex_source(
        token="mock_tok",
        include=["expenses"],
        client=mock_brex_client,
        full_refresh=True,
    )
    expenses_resource = source.selected_resources[BREX_EXPENSES_TABLE]

    with patch("dlt.current.resource_state", return_value=state):
        rows = list(expenses_resource())

    active = [r for r in rows if not r.get("_deleted")]
    tombstones = [r for r in rows if r.get("_deleted")]

    assert len(active) == 2
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "brex:expense:exp_002"
    assert tombstones[0]["_deleted"] is True
    assert state["known_ids"] == ["exp_001", "exp_003"]


def test_empty_sweep_guard_preserves_state(mock_brex_client):
    """Verify unexpected empty sweep does NOT wipe existing known records."""
    mock_brex_client.expenses = []

    state = {
        "last_posted_at": None,
        "known_ids": ["exp_001", "exp_002"],
    }

    source = brex_source(
        token="mock_tok",
        include=["expenses"],
        client=mock_brex_client,
        full_refresh=True,
        allow_empty_sweep=False,
    )
    expenses_resource = source.selected_resources[BREX_EXPENSES_TABLE]

    with patch("dlt.current.resource_state", return_value=state):
        rows = list(expenses_resource())

    # Empty sweep guard should skip deletion: 0 tombstones emitted
    assert len(rows) == 0
    # State preserved!
    assert state["known_ids"] == ["exp_001", "exp_002"]

    # When allow_empty_sweep=True is explicitly set, destructive tombstones are permitted
    source_allowed = brex_source(
        token="mock_tok",
        include=["expenses"],
        client=mock_brex_client,
        full_refresh=True,
        allow_empty_sweep=True,
    )
    expenses_resource_allowed = source_allowed.selected_resources[BREX_EXPENSES_TABLE]

    with patch("dlt.current.resource_state", return_value=state):
        rows_allowed = list(expenses_resource_allowed())

    assert len(rows_allowed) == 2
    assert all(r["_deleted"] for r in rows_allowed)
    assert state["known_ids"] == []


def test_budget_empty_sweep_guard(mock_brex_client):
    """Verify budget empty sweep guard preserves state on transient empty responses."""
    mock_brex_client.budgets = []
    state = {"known_ids": ["bgt_cloud_q2", "bgt_parent_eng"]}

    source = brex_source(
        token="mock_tok",
        include=["budgets"],
        client=mock_brex_client,
        allow_empty_sweep=False,
    )
    budgets_resource = source.selected_resources[BREX_BUDGETS_TABLE]

    with patch("dlt.current.resource_state", return_value=state):
        rows = list(budgets_resource())

    assert len(rows) == 0
    assert state["known_ids"] == ["bgt_cloud_q2", "bgt_parent_eng"]


def test_anti_corruption_guardrail_on_error():
    """Verify that an API error during sync aborts and does not emit corrupt tombstones."""
    failing_client = MagicMock()
    failing_client.fetch_all_expenses.side_effect = RuntimeError("Brex connection timeout")

    source = brex_source(
        token="mock_tok",
        include=["expenses"],
        client=failing_client,
    )
    expenses_resource = source.selected_resources[BREX_EXPENSES_TABLE]

    state = {
        "last_posted_at": None,
        "known_ids": ["exp_001", "exp_002"],
    }

    with (
        patch("dlt.current.resource_state", return_value=state),
        pytest.raises(Exception, match="Brex connection timeout"),
    ):
        list(expenses_resource())

    # State known_ids should remain intact, protecting graph nodes from deletion
    assert state["known_ids"] == ["exp_001", "exp_002"]


def test_real_dlt_pipeline_sync_and_forget_on_delete(tmp_path):
    """Run brex_source through a real dlt SQLite pipeline, verifying sync & forget-on-delete."""
    import dlt

    db_path = (tmp_path / "brex_pipeline.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="brex_pipeline_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="brex_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    sample_expenses = [
        {
            "id": "exp_alpha",
            "amount": {"amount": 1000, "currency": "USD"},
            "merchant": {"name": "Vendor Alpha"},
            "user": {"id": "usr_1", "first_name": "John", "last_name": "Doe"},
            "category": "Office",
            "memo": "First expense memo",
            "status": "APPROVED",
            "posted_at": "2026-04-01T10:00:00Z",
        },
        {
            "id": "exp_beta",
            "amount": {"amount": 2000, "currency": "USD"},
            "merchant": {"name": "Vendor Beta"},
            "user": {"id": "usr_2", "first_name": "Jane", "last_name": "Doe"},
            "category": "Travel",
            "memo": "Second expense memo",
            "status": "APPROVED",
            "posted_at": "2026-04-02T10:00:00Z",
        },
    ]

    client = MockBrexClient(expenses=list(sample_expenses), budgets=[])

    # First sync: expenses & memos
    pipeline.run(brex_source(token="tok", client=client, include=["expenses", "memos"]))

    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query("SELECT id, _deleted FROM brex_expenses") as cur,
    ):
        rows = cur.fetchall()
        loaded_ids = {r[0]: r[1] for r in rows}

    assert "brex:expense:exp_alpha" in loaded_ids
    assert "brex:expense:exp_beta" in loaded_ids
    assert loaded_ids["brex:expense:exp_alpha"] == 0

    # Verify memos table in sqlite
    with (
        pipeline.sql_client() as sql_client,
        sql_client.execute_query("SELECT id FROM brex_memos WHERE _deleted = 0") as cur,
    ):
        memo_rows = cur.fetchall()
        memo_ids = [r[0] for r in memo_rows]
    assert set(memo_ids) == {"brex:memo:exp_alpha", "brex:memo:exp_beta"}
