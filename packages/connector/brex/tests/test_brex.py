"""Unit tests for Brex connector (100% offline, mocked client and data).

Covers:
- Token validation and error handling
- Client error mappings (401, 403, 429)
- Multi-page pagination traversal
- Structured relational expenses ingestion and ID namespacing
- Document-mode routing and Markdown formatting for memos
- Structured relational budgets ingestion with parent-child links
- Incremental high-watermark cursor advancement (posted_at_start)
- Forget-on-delete tombstone emission (_deleted=True)
- Anti-corruption abort guard on failure
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

from cognee_community_connector_brex.brex import (
    BREX_BUDGETS_TABLE,
    BREX_EXPENSES_TABLE,
    BREX_MEMOS_TABLE,
    _budget_to_row,
    _expense_to_memo_card,
    _expense_to_row,
    _normalize_amount,
    brex_source,
)
from cognee_community_connector_brex.client import (
    BrexAuthError,
    BrexClient,
    BrexPermissionError,
    BrexRateLimitError,
)


def test_missing_token_raises_value_error():
    """Verify that omitting token and having no BREX_API_TOKEN raises ValueError."""
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(ValueError, match="Brex API token is required"),
    ):
        brex_source(token=None)


def test_normalize_amount():
    """Verify numeric amount and currency parsing from various Brex payload formats."""
    # Cents integer representation
    val, curr = _normalize_amount({"amount": 1050, "currency": "USD"})
    assert val == 10.50
    assert curr == "USD"

    # Float amount
    val, curr = _normalize_amount(45.25)
    assert val == 45.25
    assert curr == "USD"

    # Missing or null amount
    val, curr = _normalize_amount(None)
    assert val is None
    assert curr is None


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
    """Verify memo transformation into Markdown document card."""
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
    """Verify brex_source selects specified resources and sets DOCUMENT_SOURCE_ATTR."""
    source = brex_source(
        token="mock_tok",
        include=["expenses", "memos", "budgets"],
        client=mock_brex_client,
    )

    resources = source.selected_resources
    assert BREX_EXPENSES_TABLE in resources
    assert BREX_MEMOS_TABLE in resources
    assert BREX_BUDGETS_TABLE in resources

    # Document mode verification
    memos_res = resources[BREX_MEMOS_TABLE]
    assert getattr(memos_res, DOCUMENT_SOURCE_ATTR) == "brex"

    # Relational resources should NOT have DOCUMENT_SOURCE_ATTR set
    assert getattr(resources[BREX_EXPENSES_TABLE], DOCUMENT_SOURCE_ATTR, None) is None
    assert getattr(resources[BREX_BUDGETS_TABLE], DOCUMENT_SOURCE_ATTR, None) is None


def test_incremental_sync_and_cursor_advancement(mock_brex_client):
    """Verify incremental sync advances posted_at cursor in resource state."""
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
    # The newest timestamp in mock data is 2026-04-15T18:00:00Z
    assert state.get("last_posted_at") == "2026-04-15T18:00:00Z"


def test_forget_on_delete_tombstones(mock_brex_client, mock_expenses_data):
    """Verify missing items in full snapshot emit _deleted: True tombstones."""
    # Simulate client only returning exp_001 (exp_002 was deleted upstream)
    mock_brex_client.expenses = [mock_expenses_data[0]]

    source = brex_source(
        token="mock_tok",
        include=["expenses"],
        client=mock_brex_client,
    )
    expenses_resource = source.selected_resources[BREX_EXPENSES_TABLE]

    # Prior state knew about both exp_001 and exp_002
    state = {
        "last_posted_at": None,  # Full snapshot
        "known_ids": ["exp_001", "exp_002"],
    }

    with patch("dlt.current.resource_state", return_value=state):
        rows = list(expenses_resource())

    active_rows = [r for r in rows if not r.get("_deleted")]
    tombstone_rows = [r for r in rows if r.get("_deleted")]

    assert len(active_rows) == 1
    assert active_rows[0]["id"] == "brex:expense:exp_001"

    assert len(tombstone_rows) == 1
    assert tombstone_rows[0]["id"] == "brex:expense:exp_002"
    assert tombstone_rows[0]["_deleted"] is True


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
