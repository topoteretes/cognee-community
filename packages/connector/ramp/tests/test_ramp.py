"""Comprehensive unit tests for the Ramp data-source connector."""

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_ramp.ramp import (
    RAMP_SOURCE_NAME,
    RAMP_TABLE_NAME,
    RampClient,
    _get_retry_delay,
    _transaction_to_document,
    ramp_source,
)


def test_client_auth_missing_raises(monkeypatch):
    """Client raises ValueError when no token or OAuth client credentials are provided."""
    monkeypatch.delenv("RAMP_CLIENT_ID", raising=False)
    monkeypatch.delenv("RAMP_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("RAMP_ACCESS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Ramp authentication required"):
        RampClient()


def test_client_headers_and_bearer_auth():
    """Client sets Bearer authorization header with direct access token."""
    client = RampClient(access_token="ramp_pat_xyz")
    headers = client._get_auth_headers()
    assert headers["Authorization"] == "Bearer ramp_pat_xyz"
    client.close()


def test_client_oauth_client_credentials():
    """Client fetches OAuth access token using client credentials."""
    token_requested = False

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requested
        if "/developer/v1/token" in str(request.url):
            token_requested = True
            return httpx.Response(200, json={"access_token": "oauth_token_123"}, request=request)
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(mock_handler)
    client = RampClient(
        client_id="cid_1",
        client_secret="sec_1",
        transport=transport,
    )
    assert token_requested is True
    assert client.access_token == "oauth_token_123"
    client.close()


def test_client_oauth_token_refresh_on_401():
    """Client automatically refreshes token on 401 when client credentials are provided."""
    call_count = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        url = str(request.url)
        if "/developer/v1/token" in url:
            return httpx.Response(
                200, json={"access_token": f"refreshed_tok_{call_count}"}, request=request
            )
        if "/developer/v1/transactions" in url:
            call_count += 1
            if call_count == 1:
                return httpx.Response(401, request=request)
            return httpx.Response(200, json={"data": [{"id": "t1"}]}, request=request)
        return httpx.Response(404, request=request)

    transport = httpx.MockTransport(mock_handler)
    client = RampClient(
        client_id="cid_1",
        client_secret="sec_1",
        transport=transport,
    )
    data = client.list_transactions()
    assert len(data["data"]) == 1
    client.close()


def test_get_retry_delay():
    """Retry delay honors Retry-After header and falls back to backoff."""
    req = httpx.Request("GET", "https://api.ramp.com/test")
    resp_with_header = httpx.Response(429, headers={"retry-after": "6"}, request=req)
    assert _get_retry_delay(resp_with_header, 0) == 6.0

    resp_without_header = httpx.Response(500, request=req)
    assert _get_retry_delay(resp_without_header, 2) == 4.0
    assert _get_retry_delay(None, 1) == 2.0


def test_client_retry_on_429(monkeypatch):
    """Client retries on HTTP 429 and succeeds."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    attempts = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "0.1"}, request=request)
        return httpx.Response(200, json={"data": [{"id": "tx_ok"}]}, request=request)

    transport = httpx.MockTransport(mock_handler)
    client = RampClient(access_token="tok", transport=transport)
    res = client.list_transactions()
    assert attempts == 2
    assert res["data"][0]["id"] == "tx_ok"
    client.close()


def test_client_network_error_retry(monkeypatch):
    """Client retries on network error and succeeds."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    attempts = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.NetworkError("Ramp gateway timeout")
        return httpx.Response(200, json={"data": []}, request=request)

    transport = httpx.MockTransport(mock_handler)
    client = RampClient(access_token="tok", transport=transport)
    res = client.list_transactions()
    assert attempts == 2
    assert res["data"] == []
    client.close()


def test_client_list_transactions_params():
    """Client builds correct query parameters for transactions endpoint."""
    captured_url = None

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(200, json={"data": [], "page": {}}, request=request)

    transport = httpx.MockTransport(mock_handler)
    client = RampClient(access_token="tok", transport=transport)
    client.list_transactions(
        from_date="2026-09-01T00:00:00Z",
        to_date="2026-10-01T00:00:00Z",
        entity_id="ent_101",
        page_size=50,
        start="cursor_next",
    )

    assert "from_date=2026-09-01T00%3A00%3A00Z" in captured_url
    assert "entity_id=ent_101" in captured_url
    assert "page_size=50" in captured_url
    assert "start=cursor_next" in captured_url
    client.close()


def test_client_list_receipts_ocr():
    """Client queries receipts with include_ocr_data=true."""
    captured_url = None

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(
            200,
            json={"data": [{"id": "rec_1", "ocr_data": {"vendor_name": "AWS"}}]},
            request=request,
        )

    transport = httpx.MockTransport(mock_handler)
    client = RampClient(access_token="tok", transport=transport)
    receipts = client.list_receipts(transaction_id="tx_123", include_ocr_data=True)

    assert "transaction_id=tx_123" in captured_url
    assert "include_ocr_data=true" in captured_url
    assert len(receipts) == 1
    assert receipts[0]["ocr_data"]["vendor_name"] == "AWS"
    client.close()


def test_transaction_to_document_formatting():
    """Transaction document formats merchant, memo, and receipt line items."""
    txn = {
        "id": "txn_888",
        "merchant_name": "Google Cloud",
        "amount": 420.50,
        "currency_code": "USD",
        "state": "CLEARED",
        "user_transaction_time": "2026-10-05T12:00:00Z",
        "cardholder_name": "Alice Engineer",
        "merchant_category_code": "Cloud Services",
        "memo": "Monthly Kubernetes cluster nodes and vector database hosting.",
    }
    receipts = [
        {
            "id": "rec_999",
            "ocr_data": {
                "vendor_name": "Google Cloud EMEA",
                "total_amount": "$420.50",
                "line_items": [
                    {"description": "Compute Engine n2-standard-4", "amount": "$350.00"},
                    {"description": "Cloud Storage Buckets", "amount": "$70.50"},
                ],
            },
        }
    ]

    doc = _transaction_to_document(txn, receipts)
    assert doc["id"] == "ramp:txn:txn_888"
    assert doc["data_id"].startswith("ramp:")
    assert doc["name"] == "Google Cloud - 420.5 USD"
    assert doc["external_metadata"]["source"] == RAMP_SOURCE_NAME
    assert doc["external_metadata"]["has_memo"] is True
    assert doc["external_metadata"]["receipt_count"] == 1

    text = doc["text"]
    assert "# Ramp Transaction: Google Cloud (420.5 USD)" in text
    assert "Cardholder**: Alice Engineer" in text
    assert "Monthly Kubernetes cluster nodes" in text
    assert "Compute Engine n2-standard-4 ($350.00)" in text


def test_source_metadata_document_mode():
    """ramp_source sets cognee_document_source and DOCUMENT_SOURCE_ATTR."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, request=request)

    client = RampClient(access_token="tok", transport=httpx.MockTransport(mock_handler))
    source = ramp_source(client=client)

    assert source.cognee_document_source == "ramp"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "ramp"
    assert RAMP_TABLE_NAME in source.resources
    assert source.resources[RAMP_TABLE_NAME].write_disposition == "replace"
    client.close()


def test_source_basic_ingestion(tmp_path):
    """Source extracts transactions, receipts and loads into DuckDB."""
    tx_payload = {
        "data": [
            {
                "id": "tx_001",
                "merchant_name": "Datadog",
                "amount": 150.0,
                "currency_code": "USD",
                "memo": "Production APM monitoring",
                "user_transaction_time": "2026-10-01T10:00:00Z",
            }
        ],
        "page": {"next": None},
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/developer/v1/receipts" in url:
            return httpx.Response(200, json={"data": []}, request=request)
        if "/developer/v1/transactions" in url:
            return httpx.Response(200, json=tx_payload, request=request)
        return httpx.Response(404, request=request)

    client = RampClient(access_token="tok", transport=httpx.MockTransport(mock_handler))
    source = ramp_source(client=client)

    pipeline = dlt.pipeline(
        pipeline_name="test_ramp_pipe",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )
    load_info = pipeline.run(source)
    assert load_info.has_failed_jobs is False

    items = list(source.resources[RAMP_TABLE_NAME]())
    assert len(items) == 1
    assert items[0]["name"] == "Datadog - 150.0 USD"
    assert "Production APM monitoring" in items[0]["text"]
    client.close()


def test_source_incremental_cursor_advancement(tmp_path):
    """Source updates last_synced_time watermark cursor in dlt state."""
    tx_payload = {
        "data": [
            {
                "id": "tx_1",
                "merchant_name": "M1",
                "amount": 10,
                "user_transaction_time": "2026-10-01T10:00:00Z",
            },
            {
                "id": "tx_2",
                "merchant_name": "M2",
                "amount": 20,
                "user_transaction_time": "2026-10-05T15:00:00Z",
            },
        ],
        "page": {"next": None},
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/developer/v1/receipts" in url:
            return httpx.Response(200, json={"data": []}, request=request)
        if "/developer/v1/transactions" in url:
            return httpx.Response(200, json=tx_payload, request=request)
        return httpx.Response(404, request=request)

    client = RampClient(access_token="tok", transport=httpx.MockTransport(mock_handler))
    source = ramp_source(client=client)

    pipeline = dlt.pipeline(
        pipeline_name="test_ramp_cursor_pipe",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )
    pipeline.run(source)

    state = pipeline.state.get("sources", {}).get(RAMP_SOURCE_NAME, {})
    resource_state = state.get("resources", {}).get(RAMP_TABLE_NAME, {})
    assert resource_state.get("last_synced_time") == "2026-10-05T15:00:00Z"
    client.close()


def test_source_skip_empty_memos():
    """Source skips transactions without memos or receipts when skip_empty_memos=True."""
    tx_payload = {
        "data": [
            {"id": "t_empty", "merchant_name": "Coffee", "amount": 5.0, "memo": None},
            {"id": "t_with_memo", "merchant_name": "Lunch", "amount": 25.0, "memo": "Client sync"},
        ],
        "page": {"next": None},
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/developer/v1/receipts" in url:
            return httpx.Response(200, json={"data": []}, request=request)
        if "/developer/v1/transactions" in url:
            return httpx.Response(200, json=tx_payload, request=request)
        return httpx.Response(404, request=request)

    client = RampClient(access_token="tok", transport=httpx.MockTransport(mock_handler))
    source = ramp_source(skip_empty_memos=True, client=client)

    items = list(source.resources[RAMP_TABLE_NAME]())
    assert len(items) == 1
    assert items[0]["id"] == "ramp:txn:t_with_memo"
    client.close()


def test_source_lookback_window_applied():
    """Source queries transactions with lookback window when watermark is present."""
    captured_from = None

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_from
        url = str(request.url)
        if "/developer/v1/transactions" in url:
            captured_from = request.url.params.get("from_date")
            return httpx.Response(200, json={"data": [], "page": {}}, request=request)
        return httpx.Response(404, request=request)

    client = RampClient(access_token="tok", transport=httpx.MockTransport(mock_handler))
    source = ramp_source(from_date="2026-08-01T00:00:00Z", client=client)

    items = list(source.resources[RAMP_TABLE_NAME]())
    assert items == []
    assert captured_from == "2026-08-01T00:00:00Z"
    client.close()
