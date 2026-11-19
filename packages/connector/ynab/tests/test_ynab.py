"""Hermetic unit tests for YNAB connector."""

from typing import Any, Dict, List
import pytest
from cognee_community_connector_ynab.ynab import (
    YNABClient,
    _category_to_document,
    _milliunits_to_currency,
    _transaction_to_document,
    ynab_source,
)

SAMPLE_TRANSACTION: Dict[str, Any] = {
    "id": "tx-123456",
    "date": "2026-10-24",
    "amount": -54320,  # -$54.32
    "memo": "Team dinner & supplies",
    "cleared": "cleared",
    "approved": True,
    "payee_name": "Trader Joe's",
    "category_name": "Groceries",
    "account_name": "Checking Account",
    "subtransactions": [
        {
            "id": "sub-1",
            "amount": -40000,  # -$40.00
            "category_name": "Groceries",
            "memo": "Food supplies",
        },
        {
            "id": "sub-2",
            "amount": -14320,  # -$14.32
            "category_name": "Household",
            "memo": "Cleaning materials",
        },
    ],
}

SAMPLE_CATEGORY_GROUP: Dict[str, Any] = {
    "id": "group-001",
    "name": "Immediate Obligations",
    "categories": [
        {
            "id": "cat-rent-101",
            "name": "Rent / Mortgage",
            "budgeted": 2000000,  # $2000.00
            "activity": -2000000,  # -$2000.00
            "balance": 0,
            "goal_type": "NEED",
            "note": "Paid on 1st of every month",
            "deleted": False,
        },
        {
            "id": "cat-deleted-999",
            "name": "Old Category",
            "deleted": True,
        },
    ],
}


class FakeResponse:
    def __init__(self, json_data: Dict[str, Any], status_code: int = 200) -> None:
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}")

    def json(self) -> Dict[str, Any]:
        return self._json_data


class FakeYNABHttpClient:
    def __init__(self, routes: Dict[str, Any]) -> None:
        self.routes = routes

    def get(self, url: str, params: Any = None) -> FakeResponse:
        for path, data in self.routes.items():
            if url.endswith(path):
                return FakeResponse(data)
        return FakeResponse({}, status_code=404)


def test_milliunits_to_currency():
    """Test integer milliunits to standard currency conversion."""
    assert _milliunits_to_currency(0) == 0.0
    assert _milliunits_to_currency(1000) == 1.00
    assert _milliunits_to_currency(-54320) == -54.32
    assert _milliunits_to_currency(1234567) == 1234.57
    assert _milliunits_to_currency(None) == 0.0


def test_transaction_to_document_with_splits():
    """Test converting a split transaction into a structured Markdown document."""
    doc = _transaction_to_document(SAMPLE_TRANSACTION, budget_id="budget-primary")
    assert doc is not None
    assert doc["id"] == "ynab_tx_tx-123456"
    assert doc["title"] == "Transaction: Trader Joe's - $-54.32 (2026-10-24)"

    content = doc["content"]
    assert "# YNAB Transaction: Trader Joe's ($-54.32)" in content
    assert "**Payee:** Trader Joe's" in content
    assert "**Category:** Groceries" in content
    assert "**Account:** Checking Account" in content
    assert "**Status:** Cleared | **Review:** Approved" in content
    assert "### Split Details:" in content
    assert "**Groceries:** $-40.00 (Food supplies)" in content
    assert "**Household:** $-14.32 (Cleaning materials)" in content
    assert "budget-primary" in content


def test_category_to_document():
    """Test converting budget categories into structured documents and filtering deleted ones."""
    cat = SAMPLE_CATEGORY_GROUP["categories"][0]
    doc = _category_to_document(cat, group_name="Immediate Obligations", budget_id="budget-primary")
    assert doc is not None
    assert doc["id"] == "ynab_cat_cat-rent-101"
    assert doc["title"] == "Budget Category: Rent / Mortgage ($0.00 available)"

    content = doc["content"]
    assert "# YNAB Category: Rent / Mortgage (Group: Immediate Obligations)" in content
    assert "**Budgeted This Month:** $2000.00" in content
    assert "**Activity / Spending:** $-2000.00" in content
    assert "**Target / Goal:** NEED" in content
    assert "**Notes:** Paid on 1st of every month" in content

    # Test deleted category returns None
    deleted_cat = SAMPLE_CATEGORY_GROUP["categories"][1]
    deleted_doc = _category_to_document(deleted_cat, group_name="Immediate Obligations", budget_id="budget-primary")
    assert deleted_doc is None


def test_ynab_source_dlt_execution():
    """Test full DLT source execution with mocked YNAB API client."""
    routes = {
        "/budgets": {"data": {"budgets": [{"id": "b-1", "name": "Default Budget"}]}},
        "/budgets/b-1/transactions": {"data": {"transactions": [SAMPLE_TRANSACTION]}},
        "/budgets/b-1/categories": {"data": {"category_groups": [SAMPLE_CATEGORY_GROUP]}},
    }
    client = YNABClient(access_token="test_pat_token", http_client=FakeYNABHttpClient(routes))

    resource = ynab_source(budget_id="b-1", client=client)
    items = list(resource)

    # 1 transaction + 1 active category = 2 items
    assert len(items) == 2
    assert items[0]["id"] == "ynab_tx_tx-123456"
    assert items[1]["id"] == "ynab_cat_cat-rent-101"
