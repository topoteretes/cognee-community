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
    """Representative Brex Expenses API (v1) payload with updated_at timestamps."""
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
            "purchased_at": "2026-04-12T14:00:00Z",
            "payment_posted_at": "2026-04-12T14:30:00Z",
            "updated_at": "2026-04-12T15:00:00Z",
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
            "purchased_at": "2026-04-15T17:30:00Z",
            "payment_posted_at": "2026-04-15T18:00:00Z",
            "updated_at": "2026-04-15T18:30:00Z",
        },
    ]


@pytest.fixture
def mock_budgets_data() -> list[dict[str, Any]]:
    """Representative Brex Budgets API (v2) payload conforming to Brex OpenAPI spec."""
    return [
        {
            "budget_id": "bgt_cloud_q2",
            "name": "Q2 Cloud & Infrastructure Budget",
            "description": "Quarterly cloud hosting, SaaS tooling, and infrastructure spend.",
            "budget_type": "DEPARTMENT",
            "period_recurrence_type": "QUARTERLY",
            "status": "ACTIVE",
            "amount": {"amount": 5000000, "currency": "USD"},  # $50,000.00 in minor units
            "parent_budget_id": "bgt_parent_eng",
            "start_date": "2026-04-01",
            "end_date": "2026-06-30",
        },
        {
            "budget_id": "bgt_parent_eng",
            "name": "Engineering Master Budget",
            "description": "Annual overall budget for Engineering.",
            "budget_type": "DEPARTMENT",
            "period_recurrence_type": "YEARLY",
            "status": "ACTIVE",
            "amount": {"amount": 50000000, "currency": "USD"},  # $500,000.00
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
        updated_at_start: str | None = None,
        posted_at_start: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        sync_cursor = updated_at_start or posted_at_start
        self.calls_made.append(
            {
                "endpoint": "v1/expenses",
                "updated_at_start": sync_cursor,
                "cursor": cursor,
                "limit": limit,
            }
        )

        filtered = self.expenses
        if sync_cursor:
            filtered = [
                exp
                for exp in self.expenses
                if (
                    exp.get("updated_at")
                    or exp.get("payment_posted_at")
                    or exp.get("posted_at", "")
                )
                >= sync_cursor
            ]

        return {
            "items": filtered,
            "next_cursor": None,
        }

    def fetch_all_expenses(
        self,
        updated_at_start: str | None = None,
        posted_at_start: str | None = None,
        limit: int = 50,
    ):
        data = self.fetch_expenses(
            updated_at_start=updated_at_start,
            posted_at_start=posted_at_start,
            limit=limit,
        )
        yield from data.get("items", [])

    def fetch_budgets(
        self,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self.calls_made.append(
            {
                "endpoint": "v2/budgets",
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
