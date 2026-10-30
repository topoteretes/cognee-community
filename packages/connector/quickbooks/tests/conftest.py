from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure package root is importable regardless of test invocation method
_pkg_root = str(Path(__file__).resolve().parent.parent)
if _pkg_root not in sys.path:
    sys.path.insert(0, _pkg_root)


@pytest.fixture
def sample_invoice():
    return {
        "Id": "101",
        "DocNumber": "INV-1001",
        "TxnDate": "2024-04-01",
        "DueDate": "2024-04-30",
        "TotalAmt": 4500.0,
        "Balance": 0.0,
        "CustomerRef": {
            "value": "1",
            "name": "Acme Corp",
        },
        "CurrencyRef": {
            "value": "USD",
            "name": "United States Dollar",
        },
        "CustomerMemo": {
            "value": "Annual software renewal terms.",
        },
        "PrivateNote": "Approved by CFO.",
        "Line": [
            {
                "Id": "1",
                "DetailType": "SalesItemLineDetail",
                "Amount": 4000.0,
                "SalesItemLineDetail": {
                    "ItemRef": {"name": "Platform Subscription"},
                    "UnitPrice": 4000.0,
                    "Qty": 1,
                },
                "Description": "Annual license",
            },
            {
                "Id": "2",
                "DetailType": "SalesItemLineDetail",
                "Amount": 500.0,
                "SalesItemLineDetail": {
                    "ItemRef": {"name": "Dedicated Support"},
                    "UnitPrice": 500.0,
                    "Qty": 1,
                },
                "Description": "24/7 SLA",
            },
        ],
        "MetaData": {
            "CreateTime": "2024-04-01T08:00:00Z",
            "LastUpdatedTime": "2024-04-01T09:30:00Z",
        },
    }


@pytest.fixture
def sample_bill():
    return {
        "Id": "201",
        "DocNumber": "BILL-500",
        "TxnDate": "2024-04-05",
        "DueDate": "2024-05-05",
        "TotalAmt": 1200.0,
        "Balance": 1200.0,
        "VendorRef": {
            "value": "10",
            "name": "Cloudflare Inc",
        },
        "CurrencyRef": {
            "value": "USD",
        },
        "APAccountRef": {
            "name": "Accounts Payable",
        },
        "PrivateNote": "Enterprise DDoS and CDN",
        "Line": [
            {
                "Id": "1",
                "DetailType": "AccountBasedExpenseLineDetail",
                "Amount": 1200.0,
                "AccountBasedExpenseLineDetail": {
                    "AccountRef": {"name": "Hosting & Infrastructure"},
                },
                "Description": "Monthly CDN egress",
            }
        ],
        "MetaData": {
            "CreateTime": "2024-04-05T12:00:00Z",
            "LastUpdatedTime": "2024-04-05T12:15:00Z",
        },
    }


@pytest.fixture
def sample_credit_memo():
    return {
        "Id": "301",
        "DocNumber": "CM-10",
        "TxnDate": "2024-04-10",
        "TotalAmt": 250.0,
        "RemainingCredit": 250.0,
        "CustomerRef": {
            "value": "1",
            "name": "Acme Corp",
        },
        "CustomerMemo": {
            "value": "SLA downtime credit.",
        },
        "PrivateNote": "Internal discount approval.",
        "MetaData": {
            "CreateTime": "2024-04-10T14:00:00Z",
            "LastUpdatedTime": "2024-04-10T14:05:00Z",
        },
    }


class FakeQuickBooksClient:
    """Mock QuickBooks API client backed by in-memory data with zero network calls."""

    def __init__(
        self,
        invoices=None,
        bills=None,
        credit_memos=None,
        cdc_records=None,
        realm_id="12345",
        cdc_error: Exception | None = None,
    ):
        self.invoices = list(invoices or [])
        self.bills = list(bills or [])
        self.credit_memos = list(credit_memos or [])
        self.cdc_records = list(cdc_records or [])
        self.realm_id = realm_id
        self.cdc_error = cdc_error
        self.query_history: list[str] = []

    def fetch_all(
        self,
        entity_name: str,
        last_updated_time: str | None = None,
        page_size: int = 100,
    ):
        items_map = {
            "Invoice": self.invoices,
            "Bill": self.bills,
            "CreditMemo": self.credit_memos,
        }
        items = items_map.get(entity_name, [])

        filtered = []
        for item in items:
            meta = item.get("MetaData") or {}
            updated = meta.get("LastUpdatedTime", "")
            if last_updated_time:
                if updated >= last_updated_time:
                    filtered.append(item)
            else:
                filtered.append(item)

        yield from filtered

    def fetch_active_ids(self, entity_name: str, page_size: int = 500) -> set[str]:
        items_map = {
            "Invoice": self.invoices,
            "Bill": self.bills,
            "CreditMemo": self.credit_memos,
        }
        items = items_map.get(entity_name, [])
        return {str(item["Id"]) for item in items if "Id" in item}

    def fetch_cdc(self, entities: list[str], changed_since: str):
        if self.cdc_error:
            raise self.cdc_error
        return self.cdc_records
