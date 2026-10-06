"""Unit tests for the QuickBooks Online connector."""

import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_quickbooks.quickbooks import (
    QUICKBOOKS_BILLS_TABLE,
    QUICKBOOKS_INVOICES_TABLE,
    QUICKBOOKS_MEMOS_TABLE,
    QUICKBOOKS_SOURCE_NAME,
    QuickBooksClient,
    quickbooks_source,
)


class FakeQuickBooksClient:
    """Mock client simulating QuickBooks Online Accounting API v3."""

    def __init__(self, realm_id: str = "1234567890", responses: dict | None = None) -> None:
        self.realm_id = realm_id
        self.responses = responses or {}
        self.query_history: list[tuple[str, str]] = []

    def query(self, entity_name: str, query_str: str) -> list[dict]:
        self.query_history.append((entity_name, query_str))
        if entity_name in self.responses:
            val = self.responses[entity_name]
            if callable(val):
                return val(query_str)
            return val
        return []


def test_realm_id_required(monkeypatch):
    """Fails with ValueError if realm_id is missing and QUICKBOOKS_REALM_ID unset."""
    monkeypatch.delenv("QUICKBOOKS_REALM_ID", raising=False)
    with pytest.raises(ValueError, match="QuickBooks realmId required"):
        quickbooks_source(realm_id=None, client=None)


def test_auth_required(monkeypatch):
    """Fails with ValueError if no access token or OAuth refresh credentials given."""
    monkeypatch.setenv("QUICKBOOKS_REALM_ID", "1234567890")
    monkeypatch.delenv("QUICKBOOKS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("QUICKBOOKS_REFRESH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="QuickBooks authentication required"):
        quickbooks_source(realm_id="1234567890", client=None)


def test_source_metadata_document_mode():
    """Declares DOCUMENT_SOURCE_ATTR so Cognee processes records in document mode."""
    client = FakeQuickBooksClient()
    source = quickbooks_source(client=client)
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == QUICKBOOKS_SOURCE_NAME


def test_invoices_ingestion_and_fields():
    """Formats Invoice into proper document with line items, memos, and customer info."""
    invoice_data = [
        {
            "Id": "1001",
            "DocNumber": "INV-1001",
            "TxnDate": "2024-03-01",
            "DueDate": "2024-03-31",
            "TotalAmt": 1500.00,
            "Balance": 500.00,
            "CustomerRef": {"name": "Acme Innovations"},
            "CurrencyRef": {"value": "USD"},
            "CustomerMemo": {"value": "Net 30 payment terms"},
            "PrivateNote": "Consulting engagement phase 1",
            "Line": [
                {
                    "DetailType": "SalesItemLineDetail",
                    "Description": "AI Engineering Services",
                    "Amount": 1500.00,
                    "SalesItemLineDetail": {
                        "ItemRef": {"name": "Engineering"},
                        "Qty": 10,
                    },
                }
            ],
            "MetaData": {"LastUpdatedTime": "2024-03-02T10:00:00Z"},
        }
    ]
    client = FakeQuickBooksClient(responses={"Invoice": invoice_data})
    source = quickbooks_source(
        client=client,
        include_invoices=True,
        include_bills=False,
        include_memos=False,
    )

    inv_res = next(
        res for res in source.resources.values() if res.name == QUICKBOOKS_INVOICES_TABLE
    )
    assert inv_res.write_disposition == "replace"

    rows = list(inv_res)
    assert len(rows) == 1
    doc = rows[0]

    assert doc["id"] == "quickbooks:1234567890:invoice:1001"
    assert "INV-1001" in doc["title"]
    assert "Acme Innovations" in doc["title"]
    assert "- **Customer**: Acme Innovations" in doc["content"]
    assert "- **Total Amount**: 1500.0 USD" in doc["content"]
    assert "- **Customer Memo**: Net 30 payment terms" in doc["content"]
    assert "- **Private Note**: Consulting engagement phase 1" in doc["content"]
    assert "## Line Items" in doc["content"]
    assert "- **Engineering**: AI Engineering Services | Qty: 10 | Amount: 1500.0" in doc["content"]


def test_bills_ingestion_and_fields():
    """Formats Bill into proper document with line items, notes, and vendor info."""
    bill_data = [
        {
            "Id": "2001",
            "DocNumber": "BILL-2001",
            "TxnDate": "2024-02-15",
            "DueDate": "2024-03-15",
            "TotalAmt": 850.50,
            "Balance": 850.50,
            "VendorRef": {"name": "Cloud Hostings Inc"},
            "CurrencyRef": {"value": "USD"},
            "PrivateNote": "Monthly GPU server lease",
            "Line": [
                {
                    "DetailType": "ItemBasedExpenseLineDetail",
                    "Description": "A100 Instances",
                    "Amount": 850.50,
                    "ItemBasedExpenseLineDetail": {
                        "ItemRef": {"name": "Cloud Compute"},
                        "Qty": 1,
                    },
                }
            ],
        }
    ]
    client = FakeQuickBooksClient(responses={"Bill": bill_data})
    source = quickbooks_source(
        client=client,
        include_invoices=False,
        include_bills=True,
        include_memos=False,
    )

    bills_res = next(res for res in source.resources.values() if res.name == QUICKBOOKS_BILLS_TABLE)
    assert bills_res.write_disposition == "replace"

    rows = list(bills_res)
    assert len(rows) == 1
    doc = rows[0]

    assert doc["id"] == "quickbooks:1234567890:bill:2001"
    assert "BILL-2001" in doc["title"]
    assert "- **Vendor**: Cloud Hostings Inc" in doc["content"]
    assert "- **Total Amount**: 850.5 USD" in doc["content"]
    assert "- **Private Memo / Note**: Monthly GPU server lease" in doc["content"]
    assert "- **Cloud Compute**: A100 Instances" in doc["content"]


def test_credit_memos_ingestion_and_fields():
    """Formats CreditMemo into proper document row."""
    memo_data = [
        {
            "Id": "3001",
            "DocNumber": "CM-3001",
            "TxnDate": "2024-03-10",
            "TotalAmt": 200.00,
            "RemainingCredit": 50.00,
            "CustomerRef": {"name": "Acme Innovations"},
            "CustomerMemo": {"value": "Courtesy discount applied"},
        }
    ]
    client = FakeQuickBooksClient(responses={"CreditMemo": memo_data})
    source = quickbooks_source(
        client=client,
        include_invoices=False,
        include_bills=False,
        include_memos=True,
    )

    memos_res = next(res for res in source.resources.values() if res.name == QUICKBOOKS_MEMOS_TABLE)
    rows = list(memos_res)
    assert len(rows) == 1
    doc = rows[0]

    assert doc["id"] == "quickbooks:1234567890:creditmemo:3001"
    assert "CM-3001" in doc["title"]
    assert "- **Total Credit Amount**: 200.0" in doc["content"]
    assert "- **Remaining Credit**: 50.0" in doc["content"]
    assert "- **Customer Memo**: Courtesy discount applied" in doc["content"]


def test_incremental_sync_query_filter():
    """Constructs WHERE MetaData.LastUpdatedTime filter when since is given."""
    client = FakeQuickBooksClient(responses={"Invoice": []})
    source = quickbooks_source(
        client=client,
        include_invoices=True,
        include_bills=False,
        include_memos=False,
        since="2024-01-01T00:00:00Z",
    )
    inv_res = next(
        res for res in source.resources.values() if res.name == QUICKBOOKS_INVOICES_TABLE
    )
    list(inv_res)

    assert len(client.query_history) == 1
    entity, query_str = client.query_history[0]
    assert entity == "Invoice"
    assert "WHERE MetaData.LastUpdatedTime > '2024-01-01T00:00:00Z'" in query_str


def test_resource_state_cursor_advancement(monkeypatch):
    """Simulates dlt resource state and verifies last_updated_time advancement."""
    mock_state = {}
    import dlt

    monkeypatch.setattr(dlt.current, "resource_state", lambda: mock_state)

    invoice_data = [
        {"Id": "1", "DocNumber": "1", "MetaData": {"LastUpdatedTime": "2024-01-05T00:00:00Z"}},
        {"Id": "2", "DocNumber": "2", "MetaData": {"LastUpdatedTime": "2024-02-20T12:00:00Z"}},
    ]
    client = FakeQuickBooksClient(responses={"Invoice": invoice_data})
    source = quickbooks_source(
        client=client,
        include_invoices=True,
        include_bills=False,
        include_memos=False,
    )
    inv_res = next(
        res for res in source.resources.values() if res.name == QUICKBOOKS_INVOICES_TABLE
    )
    list(inv_res)

    assert mock_state.get("last_updated_time") == "2024-02-20T12:00:00Z"


def test_resource_selection_flags():
    """Toggles invoice, bill, and memo resources based on user flags."""
    client = FakeQuickBooksClient()

    inv_only = quickbooks_source(
        client=client,
        include_invoices=True,
        include_bills=False,
        include_memos=False,
    )
    assert QUICKBOOKS_INVOICES_TABLE in inv_only.resources
    assert QUICKBOOKS_BILLS_TABLE not in inv_only.resources
    assert QUICKBOOKS_MEMOS_TABLE not in inv_only.resources

    bills_only = quickbooks_source(
        client=client,
        include_invoices=False,
        include_bills=True,
        include_memos=False,
    )
    assert QUICKBOOKS_BILLS_TABLE in bills_only.resources
    assert QUICKBOOKS_INVOICES_TABLE not in bills_only.resources

    memos_only = quickbooks_source(
        client=client,
        include_invoices=False,
        include_bills=False,
        include_memos=True,
    )
    assert QUICKBOOKS_MEMOS_TABLE in memos_only.resources
    assert QUICKBOOKS_INVOICES_TABLE not in memos_only.resources


def test_filter_by_specific_ids():
    """Filters results to specified invoice_ids, bill_ids, memo_ids."""
    invoices = [
        {"Id": "inv_1", "DocNumber": "1"},
        {"Id": "inv_2", "DocNumber": "2"},
        {"Id": "inv_3", "DocNumber": "3"},
    ]
    client = FakeQuickBooksClient(responses={"Invoice": invoices})
    source = quickbooks_source(
        client=client,
        include_invoices=True,
        include_bills=False,
        include_memos=False,
        invoice_ids=["inv_1", "inv_3"],
    )
    inv_res = next(
        res for res in source.resources.values() if res.name == QUICKBOOKS_INVOICES_TABLE
    )
    rows = list(inv_res)
    assert len(rows) == 2
    assert rows[0]["id"] == "quickbooks:1234567890:invoice:inv_1"
    assert rows[1]["id"] == "quickbooks:1234567890:invoice:inv_3"


def test_client_query_auto_pagination():
    """QuickBooksClient.query paginates through results using STARTPOSITION and MAXRESULTS."""
    calls = []

    def fake_get(endpoint, params=None):
        query = params.get("query", "")
        calls.append(query)
        if "STARTPOSITION 101" in query:
            return {
                "QueryResponse": {
                    "Invoice": [{"Id": f"i_{n}"} for n in range(100, 125)],
                }
            }
        elif "STARTPOSITION 1 " in query:
            return {
                "QueryResponse": {
                    "Invoice": [{"Id": f"i_{n}"} for n in range(100)],
                }
            }
        return {"QueryResponse": {}}

    client = QuickBooksClient(realm_id="123456", access_token="test_tok")
    client._get = fake_get

    results = client.query("Invoice", "SELECT * FROM Invoice")
    assert len(results) == 125
    assert len(calls) == 2
    assert "STARTPOSITION 1 MAXRESULTS 100" in calls[0]
    assert "STARTPOSITION 101 MAXRESULTS 100" in calls[1]


def test_oauth_token_refresh(monkeypatch):
    """Refreshes access token via Intuit OAuth 2.0 token endpoint."""

    def mock_post(url, auth, data, headers):
        assert "oauth2/v1/tokens/bearer" in url
        assert auth == ("my_client_id", "my_secret")
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "old_refresh_token"

        class MockResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "access_token": "new_access_token_123",
                    "refresh_token": "new_refresh_token_456",
                }

        return MockResponse()

    import httpx

    monkeypatch.setattr(httpx, "post", mock_post)

    client = QuickBooksClient(
        realm_id="123",
        refresh_token="old_refresh_token",
        client_id="my_client_id",
        client_secret="my_secret",
    )
    assert client.access_token == "new_access_token_123"
    assert client.refresh_token == "new_refresh_token_456"


def test_retry_after_and_transient_helpers():
    """Validates _retry_after header parsing and transient exception identification."""
    import httpx

    from cognee_community_connector_quickbooks.quickbooks import _is_transient, _retry_after

    assert _retry_after({"retry-after": "7"}, 0) == 7.0
    assert _retry_after({"Retry-After": "15"}, 0) == 15.0
    assert _retry_after({}, 3) == 8.0

    assert _is_transient(httpx.ConnectTimeout("connection timed out")) is True
    assert _is_transient(httpx.NetworkError("network failure")) is True
    assert _is_transient(ValueError("invalid logic")) is False
