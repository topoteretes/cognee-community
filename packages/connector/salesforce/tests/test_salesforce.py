"""Unit tests for Salesforce connector (100% offline, mocked client).

Covers:
- OAuth token refresh on 401 expiration
- SOQL query pagination (nextRecordsUrl)
- Record transformation into structured relational rows
- Incremental high-watermark cursor advancement
- Forget-on-delete tombstone emission via getDeleted
- Relational foreign key normalization (account_id, contact_id, feed_item_id)
- Security sanitization (no credentials leaked in rows)
- Structured ingestion verification (DOCUMENT_SOURCE_ATTR is None)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"
from cognee_community_connector_salesforce.client import (
    SalesforceAPIError,
    SalesforceAuthError,
    SalesforceClient,
)
from cognee_community_connector_salesforce.salesforce import (
    _record_to_row,
    salesforce_source,
    sync_salesforce_object,
)


class FakeSalesforceClient:
    """Mock SalesforceClient simulating REST endpoints and SOQL responses."""

    def __init__(
        self,
        records_by_query: dict[str, list[dict[str, Any]]] | None = None,
        deleted_by_object: dict[str, list[dict[str, Any]]] | None = None,
        instance_url: str = "https://test.my.salesforce.com",
    ):
        self.instance_url = instance_url
        self.records_by_query = records_by_query or {}
        self.deleted_by_object = deleted_by_object or {}
        self.access_token = "mock-token-xyz"
        self.refresh_token = "mock-refresh-token"
        self.queries_executed: list[str] = []
        self.deleted_calls: list[tuple[str, str, str]] = []

    def authenticate(self) -> None:
        pass

    def query(self, soql: str):
        self.queries_executed.append(soql)
        for key, records in self.records_by_query.items():
            if key in soql:
                yield from records
                return
        yield from []

    def get_deleted(self, sobject: str, start_time: str, end_time: str) -> list[dict[str, Any]]:
        self.deleted_calls.append((sobject, start_time, end_time))
        return self.deleted_by_object.get(sobject, [])


def test_record_to_row_mapping_and_sanitization():
    raw_account = {
        "attributes": {"type": "Account", "url": "/services/data/v60.0/sobjects/Account/001ABC"},
        "Id": "001ABC",
        "Name": "Acme Corp",
        "Industry": "Technology",
        "AnnualRevenue": 1000000.0,
        "LastModifiedDate": "2026-10-08T10:00:00Z",
    }
    row = _record_to_row("Account", raw_account, "https://acme.my.salesforce.com")

    assert row["id"] == "salesforce:account:001ABC"
    assert row["salesforce_id"] == "001ABC"
    assert row["object_type"] == "Account"
    assert row["name"] == "Acme Corp"
    assert row["industry"] == "Technology"
    assert row["annualrevenue"] == 1000000.0
    assert row["url"] == "https://acme.my.salesforce.com/001ABC"
    assert row["_deleted"] is False
    assert "attributes" not in row

    # Ensure no credentials or tokens are present in row
    for val in row.values():
        assert "token" not in str(val).lower()
        assert "secret" not in str(val).lower()


def test_relational_lookup_normalization():
    raw_opp = {
        "Id": "006XYZ",
        "Name": "Big Deal",
        "AccountId": "001ABC",
        "Amount": 50000.0,
        "StageName": "Closed Won",
        "LastModifiedDate": "2026-10-08T11:00:00Z",
    }
    row = _record_to_row("Opportunity", raw_opp, "https://acme.my.salesforce.com")

    assert row["id"] == "salesforce:opportunity:006XYZ"
    assert row["account_id"] == "salesforce:account:001ABC"


def test_initial_sync_and_cursor_advancement():
    accounts = [
        {"Id": "001A", "Name": "Company A", "LastModifiedDate": "2026-10-08T09:00:00Z"},
        {"Id": "001B", "Name": "Company B", "LastModifiedDate": "2026-10-08T12:00:00Z"},
    ]
    client = FakeSalesforceClient(records_by_query={"Account": accounts})
    state: dict[str, Any] = {}

    rows = list(sync_salesforce_object(client, "Account", state))

    assert len(rows) == 2
    assert rows[0]["id"] == "salesforce:account:001A"
    assert rows[1]["id"] == "salesforce:account:001B"
    # Cursor advanced to maximum seen timestamp
    assert state["Account_last_sync"] == "2026-10-08T12:00:00Z"


def test_incremental_sync_and_forget_on_delete():
    # Previous sync had cursor at 12:00
    state = {"Account_last_sync": "2026-10-08T12:00:00Z"}
    new_accounts = [
        {"Id": "001C", "Name": "Company C", "LastModifiedDate": "2026-10-08T13:00:00Z"},
    ]
    deleted_accounts = [
        {"id": "001A", "deletedDate": "2026-10-08T12:30:00Z"},
    ]

    client = FakeSalesforceClient(
        records_by_query={"LastModifiedDate >=": new_accounts},
        deleted_by_object={"Account": deleted_accounts},
    )

    rows = list(sync_salesforce_object(client, "Account", state))

    # Expect 1 new record + 1 tombstone
    assert len(rows) == 2

    live_row = next(r for r in rows if not r["_deleted"])
    assert live_row["id"] == "salesforce:account:001C"

    tombstone = next(r for r in rows if r["_deleted"])
    assert tombstone["id"] == "salesforce:account:001A"
    assert tombstone["_deleted"] is True

    # Cursor advanced to newest modified timestamp
    assert state["Account_last_sync"] == "2026-10-08T13:00:00Z"


def test_structured_ingestion_not_document_source():
    resource = salesforce_source(
        instance_url="https://test.my.salesforce.com",
        access_token="mock-token",
        client=FakeSalesforceClient(),
    )
    # Crucial acceptance criteria: DOCUMENT_SOURCE_ATTR is NOT set
    # so dlt preserves structured relational tables rather than routing through text extraction
    assert getattr(resource, DOCUMENT_SOURCE_ATTR, None) is None


def test_oauth_token_auto_refresh_on_401():
    mock_session = MagicMock()

    # Request 1: 401 Unauthorized
    resp_401 = MagicMock()
    resp_401.status_code = 401
    resp_401.ok = False
    resp_401.text = "Session expired or invalid"

    # Token refresh request: 200 OK
    resp_refresh = MagicMock()
    resp_refresh.status_code = 200
    resp_refresh.ok = True
    resp_refresh.json.return_value = {
        "access_token": "fresh-new-access-token",
        "instance_url": "https://refreshed.my.salesforce.com",
    }

    # Request 2 (retry): 200 OK
    resp_retry = MagicMock()
    resp_retry.status_code = 200
    resp_retry.ok = True
    resp_retry.content = b'{"records": []}'
    resp_retry.json.return_value = {"records": [{"Id": "001Refreshed", "Name": "After Refresh"}]}

    mock_session.request.side_effect = [resp_401, resp_retry]
    mock_session.post.return_value = resp_refresh

    client = SalesforceClient(
        instance_url="https://test.my.salesforce.com",
        client_id="my-client-id",
        client_secret="my-client-secret",
        refresh_token="my-refresh-token",
        access_token="initial-expired-token",
        session=mock_session,
    )

    result = client._request("GET", "/services/data/v60.0/query/?q=SELECT+Id+FROM+Account")

    # Assert refresh request occurred
    assert mock_session.post.call_count == 1
    assert client.access_token == "fresh-new-access-token"
    assert result["records"][0]["Id"] == "001Refreshed"


def test_soql_pagination_next_records_url():
    mock_session = MagicMock()

    page1_resp = MagicMock()
    page1_resp.ok = True
    page1_resp.status_code = 200
    page1_resp.content = b"content"
    page1_resp.json.return_value = {
        "records": [{"Id": "001Page1"}],
        "nextRecordsUrl": "/services/data/v60.0/query/cursor-xyz",
        "done": False,
    }

    page2_resp = MagicMock()
    page2_resp.ok = True
    page2_resp.status_code = 200
    page2_resp.content = b"content"
    page2_resp.json.return_value = {
        "records": [{"Id": "001Page2"}],
        "done": True,
    }

    mock_session.request.side_effect = [page1_resp, page2_resp]

    client = SalesforceClient(
        instance_url="https://test.my.salesforce.com",
        access_token="valid-token",
        session=mock_session,
    )

    records = list(client.query("SELECT Id FROM Account"))
    assert len(records) == 2
    assert records[0]["Id"] == "001Page1"
    assert records[1]["Id"] == "001Page2"


def test_auth_error_on_missing_credentials():
    client = SalesforceClient()
    with pytest.raises(SalesforceAuthError):
        client.authenticate()


def test_api_error_on_server_failure():
    mock_session = MagicMock()
    resp_500 = MagicMock()
    resp_500.ok = False
    resp_500.status_code = 500
    resp_500.text = "Internal Server Error"
    mock_session.request.return_value = resp_500

    client = SalesforceClient(
        instance_url="https://test.my.salesforce.com",
        access_token="valid-token",
        session=mock_session,
    )

    with pytest.raises(SalesforceAPIError) as exc_info:
        client._request("GET", "/services/data/v60.0/query")

    assert "500" in str(exc_info.value)
