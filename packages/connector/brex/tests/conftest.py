"""Test fixtures and mock helpers for Brex connector offline unit tests."""

from __future__ import annotations

import pathlib
import sys
from typing import Any

import pytest

# Ensure connector package is in python search path
package_root = pathlib.Path(__file__).resolve().parents[1]
if str(package_root) not in sys.path:
    sys.path.insert(0, str(package_root))


@pytest.fixture
def sample_token() -> str:
    return "b_usr_test_token_secret_123"


@pytest.fixture
def mock_expenses_data() -> list[dict[str, Any]]:
    return [
        {
            "id": "exp_001",
            "amount": {"amount": 342000, "currency": "USD"},  # $3,420.00 in cents
            "merchant": {"name": "Datadog Inc", "raw_descriptor": "DATADOG*MONITORING"},
            "user": {"id": "usr_001", "first_name": "Jane", "last_name": "Doe"},
            "department": {"id": "dept_eng", "name": "Engineering"},
            "category": "Software & Cloud Services",
            "memo": "Monthly infrastructure log indexing and Kubernetes cluster monitoring.",
            "status": "APPROVED",
            "budget_id": "bgt_cloud_q2",
            "posted_at": "2026-04-12T14:30:00Z",
            "purchased_at": "2026-04-12T14:00:00Z",
        },
        {
            "id": "exp_002",
            "amount": {"amount": 1500000, "currency": "USD"},  # $15,000.00
            "merchant": {"name": "Amazon Web Services", "raw_descriptor": "AWS*CLOUD"},
            "user": {"id": "usr_002", "first_name": "Alex", "last_name": "Smith"},
            "department": {"id": "dept_eng", "name": "Engineering"},
            "category": "Cloud Infrastructure",
            "memo": "Production compute and database hosting.",
            "status": "APPROVED",
            "budget_id": "bgt_cloud_q2",
            "posted_at": "2026-04-15T18:00:00Z",
            "purchased_at": "2026-04-15T17:30:00Z",
        },
    ]


@pytest.fixture
def mock_budgets_data() -> list[dict[str, Any]]:
    return [
        {
            "id": "bgt_cloud_q2",
            "name": "Q2 Cloud & Infrastructure Budget",
            "description": "Quarterly cloud hosting, SaaS tooling, and infrastructure spend.",
            "budget_type": "DEPARTMENT",
            "period_type": "QUARTERLY",
            "limit": {"amount": 5000000, "currency": "USD"},  # $50,000.00
            "spent": {"amount": 1842000, "currency": "USD"},  # $18,420.00
            "parent_budget_id": "bgt_parent_eng",
            "start_date": "2026-04-01",
            "end_date": "2026-06-30",
        },
        {
            "id": "bgt_parent_eng",
            "name": "Engineering Master Budget",
            "description": "Annual overall budget for Engineering.",
            "budget_type": "DEPARTMENT",
            "period_type": "YEARLY",
            "limit": {"amount": 50000000, "currency": "USD"},  # $500,000.00
            "spent": {"amount": 12000000, "currency": "USD"},  # $120,000.00
            "parent_budget_id": None,
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        },
    ]


class MockBrexClient:
    """Mock client simulating Brex endpoints for 100% offline testing."""

    def __init__(
        self,
        expenses: list[dict[str, Any]] | None = None,
        budgets: list[dict[str, Any]] | None = None,
        token: str = "b_usr_mock_token",
    ) -> None:
        self.token = token
        self.expenses = expenses or []
        self.budgets = budgets or []
        self.calls_made: list[dict[str, Any]] = []

    def fetch_expenses(
        self,
        posted_at_start: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self.calls_made.append(
            {
                "endpoint": "expenses/card",
                "posted_at_start": posted_at_start,
                "cursor": cursor,
                "limit": limit,
            }
        )

        filtered = self.expenses
        if posted_at_start:
            filtered = [exp for exp in self.expenses if exp.get("posted_at", "") >= posted_at_start]

        return {
            "items": filtered,
            "next_cursor": None,
        }

    def fetch_all_expenses(
        self,
        posted_at_start: str | None = None,
        limit: int = 50,
    ):
        data = self.fetch_expenses(posted_at_start=posted_at_start, limit=limit)
        yield from data.get("items", [])

    def fetch_budgets(
        self,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self.calls_made.append(
            {
                "endpoint": "budgets",
                "cursor": cursor,
                "limit": limit,
            }
        )
        return {
            "items": self.budgets,
            "next_cursor": None,
        }

    def fetch_all_budgets(self, limit: int = 50):
        data = self.fetch_budgets(limit=limit)
        yield from data.get("items", [])


@pytest.fixture
def mock_brex_client(
    mock_expenses_data: list[dict[str, Any]],
    mock_budgets_data: list[dict[str, Any]],
) -> MockBrexClient:
    return MockBrexClient(expenses=mock_expenses_data, budgets=mock_budgets_data)
