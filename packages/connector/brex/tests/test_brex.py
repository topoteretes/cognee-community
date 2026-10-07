"""Comprehensive unit tests for the Brex data-source connector."""

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_brex.brex import (
    BREX_SOURCE_NAME,
    BREX_TABLE_NAME,
    BrexClient,
    _budget_to_document,
    _expense_to_document,
    _get_retry_delay,
    brex_source,
)


def test_client_auth_missing_raises(monkeypatch):
    """Client raises ValueError when no API token is provided or in environment."""
    monkeypatch.delenv("BREX_API_KEY", raising=False)
    monkeypatch.delenv("BREX_ACCESS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Brex API key required"):
        BrexClient()


def test_client_headers_and_bearer_auth():
    """Client configures Bearer token and custom user agent."""
    client = BrexClient(api_key="brex_token_999")
    assert client.client.headers["authorization"] == "Bearer brex_token_999"
    assert "cognee-community-connector-brex" in client.client.headers["user-agent"]
    client.close()


def test_get_retry_delay():
    """Retry delay honors Retry-After header and falls back to backoff."""
    req = httpx.Request("GET", "https://platform.brexapis.com/test")
    resp_with_header = httpx.Response(429, headers={"retry-after": "4"}, request=req)
    assert _get_retry_delay(resp_with_header, 0) == 4.0

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
        return httpx.Response(200, json={"items": [{"id": "exp_ok"}]}, request=request)

    transport = httpx.MockTransport(mock_handler)
    client = BrexClient(api_key="tok", transport=transport)
    res = client.list_expenses()
    assert attempts == 2
    assert res["items"][0]["id"] == "exp_ok"
    client.close()


def test_client_network_error_retry(monkeypatch):
    """Client retries on network error and succeeds."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    attempts = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.NetworkError("Brex API connection reset")
        return httpx.Response(200, json={"items": []}, request=request)

    transport = httpx.MockTransport(mock_handler)
    client = BrexClient(api_key="tok", transport=transport)
    res = client.list_expenses()
    assert attempts == 2
    assert res["items"] == []
    client.close()


def test_client_list_expenses_pagination():
    """Client queries expenses with cursor pagination."""
    captured_urls = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return httpx.Response(200, json={"items": [], "next_cursor": None}, request=request)

    transport = httpx.MockTransport(mock_handler)
    client = BrexClient(api_key="tok", transport=transport)
    client.list_expenses(
        posted_at_start="2026-10-01T00:00:00Z",
        posted_at_end="2026-10-05T00:00:00Z",
        cursor="cur_1",
        limit=50,
    )

    assert len(captured_urls) == 1
    url = captured_urls[0]
    assert "posted_at_start=2026-10-01T00%3A00%3A00Z" in url
    assert "posted_at_end=2026-10-05T00%3A00%3A00Z" in url
    assert "cursor=cur_1" in url
    assert "limit=50" in url
    client.close()


def test_client_list_budgets_pagination():
    """Client queries budgets endpoint with cursor."""
    captured_urls = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return httpx.Response(200, json={"items": []}, request=request)

    transport = httpx.MockTransport(mock_handler)
    client = BrexClient(api_key="tok", transport=transport)
    client.list_budgets(cursor="cur_b", limit=20)

    assert "cursor=cur_b" in captured_urls[0]
    assert "limit=20" in captured_urls[0]
    client.close()


def test_expense_to_document_formatting():
    """Expense document formats merchant, amount, memo, and receipts."""
    expense = {
        "id": "exp_101",
        "merchant_name": "OpenAI",
        "amount": {"amount": 500.0, "currency": "USD"},
        "status": "POSTED",
        "posted_at": "2026-10-02T14:00:00Z",
        "category": "Software & AI",
        "memo": "Enterprise API subscription for production LLM workload.",
        "cardholder": {"name": "Bob Architect", "email": "bob@org.com"},
        "receipts": [{"name": "openai_invoice_oct.pdf"}],
    }

    doc = _expense_to_document(expense)
    assert doc["id"] == "brex:expense:exp_101"
    assert doc["data_id"].startswith("brex:")
    assert doc["name"] == "OpenAI - 500.0 USD"
    assert doc["external_metadata"]["source"] == BREX_SOURCE_NAME
    assert doc["external_metadata"]["has_memo"] is True

    text = doc["text"]
    assert "# Brex Expense: OpenAI (500.0 USD)" in text
    assert "Cardholder**: Bob Architect" in text
    assert "Enterprise API subscription for production LLM workload." in text
    assert "openai_invoice_oct.pdf" in text


def test_budget_to_document_formatting():
    """Budget document formats name, limit, spent, and description."""
    budget = {
        "id": "b_eng_q4",
        "name": "Engineering Infrastructure Q4",
        "description": "Cloud hosting, databases, and continuous integration runners.",
        "period": "QUARTERLY",
        "limit": {"amount": 50000.0, "currency": "USD"},
        "spent": {"amount": 12500.0, "currency": "USD"},
    }

    doc = _budget_to_document(budget)
    assert doc["id"] == "brex:budget:b_eng_q4"
    assert doc["data_id"].startswith("brex:")
    assert doc["name"] == "Budget: Engineering Infrastructure Q4"
    assert doc["external_metadata"]["source"] == BREX_SOURCE_NAME

    text = doc["text"]
    assert "# Brex Budget: Engineering Infrastructure Q4" in text
    assert "50000.0 USD" in text
    assert "12500.0 USD" in text
    assert "Cloud hosting, databases, and continuous integration runners." in text


def test_source_metadata_document_mode():
    """brex_source declares cognee_document_source and DOCUMENT_SOURCE_ATTR."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, request=request)

    client = BrexClient(api_key="tok", transport=httpx.MockTransport(mock_handler))
    source = brex_source(client=client)

    assert source.cognee_document_source == "brex"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "brex"
    assert BREX_TABLE_NAME in source.resources
    assert source.resources[BREX_TABLE_NAME].write_disposition == "replace"
    client.close()


def test_source_basic_ingestion(tmp_path):
    """Source executes through dlt pipeline and loads expenses and budgets."""
    exp_payload = {
        "items": [
            {
                "id": "exp_01",
                "merchant_name": "GitHub",
                "amount": {"amount": 42.0, "currency": "USD"},
                "memo": "Copilot for team",
                "posted_at": "2026-10-01T09:00:00Z",
            }
        ],
        "next_cursor": None,
    }
    budget_payload = {
        "items": [
            {
                "id": "b_01",
                "name": "Developer Tools",
                "description": "IDEs and AI coding tools",
                "limit": {"amount": 1000.0, "currency": "USD"},
            }
        ],
        "next_cursor": None,
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v2/expenses/card" in url:
            return httpx.Response(200, json=exp_payload, request=request)
        if "/v1/budgets" in url:
            return httpx.Response(200, json=budget_payload, request=request)
        return httpx.Response(404, request=request)

    client = BrexClient(api_key="tok", transport=httpx.MockTransport(mock_handler))
    source = brex_source(client=client)

    pipeline = dlt.pipeline(
        pipeline_name="test_brex_pipe",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )
    load_info = pipeline.run(source)
    assert load_info.has_failed_jobs is False

    items = list(source.resources[BREX_TABLE_NAME]())
    assert len(items) == 2
    names = [i["name"] for i in items]
    assert "GitHub - 42.0 USD" in names
    assert "Budget: Developer Tools" in names
    client.close()


def test_source_incremental_cursor_advancement(tmp_path):
    """Source advances last_posted_at watermark cursor in dlt state."""
    exp_payload = {
        "items": [
            {"id": "e1", "merchant_name": "M1", "posted_at": "2026-10-01T00:00:00Z"},
            {"id": "e2", "merchant_name": "M2", "posted_at": "2026-10-06T18:00:00Z"},
        ],
        "next_cursor": None,
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v2/expenses/card" in url:
            return httpx.Response(200, json=exp_payload, request=request)
        if "/v1/budgets" in url:
            return httpx.Response(200, json={"items": []}, request=request)
        return httpx.Response(404, request=request)

    client = BrexClient(api_key="tok", transport=httpx.MockTransport(mock_handler))
    source = brex_source(client=client)

    pipeline = dlt.pipeline(
        pipeline_name="test_brex_cursor_pipe",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )
    pipeline.run(source)

    state = pipeline.state.get("sources", {}).get(BREX_SOURCE_NAME, {})
    resource_state = state.get("resources", {}).get(BREX_TABLE_NAME, {})
    assert resource_state.get("last_posted_at") == "2026-10-06T18:00:00Z"
    client.close()


def test_source_selection_flags():
    """Source respects include_expenses and include_budgets flags."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/budgets" in url:
            return httpx.Response(
                200, json={"items": [{"id": "b_only", "name": "B"}]}, request=request
            )
        return httpx.Response(404, request=request)

    client = BrexClient(api_key="tok", transport=httpx.MockTransport(mock_handler))
    source = brex_source(include_expenses=False, include_budgets=True, client=client)

    items = list(source.resources[BREX_TABLE_NAME]())
    assert len(items) == 1
    assert items[0]["id"] == "brex:budget:b_only"
    client.close()


def test_source_custom_date_filter():
    """Source uses posted_at_start parameter when querying expenses."""
    captured_start = None

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_start
        url = str(request.url)
        if "/v2/expenses/card" in url:
            captured_start = request.url.params.get("posted_at_start")
            return httpx.Response(200, json={"items": []}, request=request)
        if "/v1/budgets" in url:
            return httpx.Response(200, json={"items": []}, request=request)
        return httpx.Response(404, request=request)

    client = BrexClient(api_key="tok", transport=httpx.MockTransport(mock_handler))
    source = brex_source(posted_at_start="2026-09-15T00:00:00Z", client=client)

    items = list(source.resources[BREX_TABLE_NAME]())
    assert items == []
    assert captured_start == "2026-09-15T00:00:00Z"
    client.close()


def test_expense_to_document_without_memo():
    """Expense without memo formats cleanly without business purpose section."""
    expense = {
        "id": "exp_nomemo",
        "merchant_name": "Lyft",
        "amount": {"amount": 25.50, "currency": "USD"},
        "status": "POSTED",
        "posted_at": "2026-10-04T22:00:00Z",
        "memo": None,
    }
    doc = _expense_to_document(expense)
    assert doc["id"] == "brex:expense:exp_nomemo"
    assert doc["external_metadata"]["has_memo"] is False
    assert "Business Purpose Memo" not in doc["text"]
