from __future__ import annotations

from pathlib import Path

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from cognee_community_connector_nocodb.nocodb import (
    NOCODB_TABLE_NAME,
    NocoDBClient,
    fetch_nocodb_records,
    get_retry_delay,
    nocodb_source,
    record_to_document,
)


def test_client_auth_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOCODB_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="NocoDB API token is required"):
        NocoDBClient(api_token=None)


def test_client_headers_custom_base_url() -> None:
    client = NocoDBClient(api_token="xc_token_123", base_url="https://nocodb.company.com/")
    assert client.base_url == "https://nocodb.company.com"
    assert client.client.headers["xc-token"] == "xc_token_123"
    client.close()


def test_get_retry_delay() -> None:
    resp_with_header = httpx.Response(429, headers={"Retry-After": "2.8"})
    assert get_retry_delay(resp_with_header, 0) == 2.8

    resp_no_header = httpx.Response(429)
    assert get_retry_delay(resp_no_header, 1, base_delay=1.0) == 2.0


def test_client_retry_on_429() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json={"list": [{"Id": 1, "Title": "Item 1"}]})

    transport = httpx.MockTransport(handler)
    with NocoDBClient(api_token="test_tok", transport=transport) as client:
        res = client.list_records(table_id="tbl_1")
        assert len(res["list"]) == 1
        assert call_count == 2


def test_client_network_error_retry() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.NetworkError("Reset connection")
        return httpx.Response(200, json={"list": [{"Id": 2, "Title": "Recovered"}]})

    transport = httpx.MockTransport(handler)
    with NocoDBClient(api_token="test_tok", transport=transport) as client:
        res = client.list_records(table_id="tbl_1")
        assert len(res["list"]) == 1
        assert call_count == 2


def test_client_list_records_params() -> None:
    captured_url: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(200, json={"list": []})

    transport = httpx.MockTransport(handler)
    with NocoDBClient(api_token="test_tok", transport=transport) as client:
        client.list_records(
            table_id="tbl_inventory",
            offset=100,
            limit=50,
            where="(Status,eq,Active)",
            view_id="vw_main",
        )

    assert captured_url is not None
    assert "/api/v2/tables/tbl_inventory/records" in captured_url
    assert "offset=100" in captured_url
    assert "limit=50" in captured_url
    assert "where" in captured_url and "Status" in captured_url
    assert "viewId=vw_main" in captured_url


def test_record_to_document_formatting() -> None:
    raw_record = {
        "Id": 105,
        "Title": "Acme Hardware Supplier",
        "Category": "Electronics",
        "Country": "Germany",
        "LeadTimeDays": 14,
        "CreatedAt": "2026-10-01T10:00:00Z",
        "UpdatedAt": "2026-10-02T12:00:00Z",
    }

    doc = record_to_document(raw_record, "tbl_suppliers")
    assert doc["id"] == "tbl_suppliers_105"
    assert doc["title"] == "Acme Hardware Supplier"
    assert doc["metadata"]["source"] == "nocodb"
    assert doc["metadata"]["table_id"] == "tbl_suppliers"

    text = doc["text"]
    assert "# Acme Hardware Supplier" in text
    assert "- **Table ID**: tbl_suppliers" in text
    assert "- **Category**: Electronics" in text
    assert "- **Country**: Germany" in text
    assert "- **LeadTimeDays**: 14" in text


def test_record_to_document_fallback_title() -> None:
    raw_record = {
        "Id": 999,
        "SomeField": "Random value",
    }
    doc = record_to_document(raw_record, "tbl_anon")
    assert doc["title"] == "NocoDB Row #999"
    assert "# NocoDB Row #999" in doc["text"]


def test_source_metadata_document_mode() -> None:
    source = nocodb_source(api_token="dummy_token")
    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == "nocodb"


def test_source_basic_ingestion(tmp_path: Path) -> None:
    mock_records = [
        {"Id": 1, "Title": "First Row", "UpdatedAt": "2026-10-01T10:00:00Z"},
        {"Id": 2, "Title": "Second Row", "UpdatedAt": "2026-10-02T10:00:00Z"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"list": mock_records, "pageInfo": {"isLastPage": True}})

    transport = httpx.MockTransport(handler)
    pipeline = dlt.pipeline(
        pipeline_name="test_nocodb_pipeline",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )

    source = nocodb_source(table_ids="tbl_main", api_token="dummy_tok", transport=transport)
    info = pipeline.run(source)
    assert info.has_failed_jobs is False

    with pipeline.sql_client() as client:
        with client.execute_query(f"SELECT COUNT(*) FROM {NOCODB_TABLE_NAME}") as cursor:
            count = cursor.fetchone()[0]
            assert count == 2


def test_source_incremental_cursor_advancement(tmp_path: Path) -> None:
    batch1 = {"list": [{"Id": 10, "Title": "Old Row", "UpdatedAt": "2026-10-01T12:00:00Z"}]}
    batch2 = {"list": [{"Id": 20, "Title": "New Row", "UpdatedAt": "2026-10-06T15:00:00Z"}]}

    last_where_param: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal last_where_param
        where_val = request.url.params.get("where")
        last_where_param = where_val
        if where_val == "(UpdatedAt,gt,2026-10-01T12:00:00Z)":
            return httpx.Response(200, json=batch2)
        return httpx.Response(200, json=batch1)

    transport = httpx.MockTransport(handler)
    pipeline = dlt.pipeline(
        pipeline_name="test_nocodb_incremental",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )

    # First run
    source1 = nocodb_source(
        table_ids="tbl_inc",
        api_token="dummy_tok",
        incremental=True,
        transport=transport,
    )
    pipeline.run(source1)

    # Second run
    source2 = nocodb_source(
        table_ids="tbl_inc",
        api_token="dummy_tok",
        incremental=True,
        transport=transport,
    )
    pipeline.run(source2)

    assert last_where_param == "(UpdatedAt,gt,2026-10-01T12:00:00Z)"

    with pipeline.sql_client() as client:
        with client.execute_query(f"SELECT COUNT(*) FROM {NOCODB_TABLE_NAME}") as cursor:
            total = cursor.fetchone()[0]
            assert total == 2


def test_source_multiple_tables() -> None:
    tables_called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tables_called.append(request.url.path)
        return httpx.Response(200, json={"list": []})

    transport = httpx.MockTransport(handler)
    docs = list(
        fetch_nocodb_records(
            table_ids=["t1", "t2"],
            api_token="dummy_tok",
            transport=transport,
        )
    )
    assert docs == []
    assert any("tables/t1/records" in p for p in tables_called)
    assert any("tables/t2/records" in p for p in tables_called)


def test_client_context_manager() -> None:
    with NocoDBClient(api_token="dummy_tok") as client:
        assert client.client.is_closed is False
    assert client.client.is_closed is True


def test_client_server_error_retry_exhaustion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(handler)
    with NocoDBClient(api_token="dummy_tok", max_retries=1, transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.list_records(table_id="tbl_err")


def test_pagination_is_last_page_break() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={
                "list": [{"Id": 1}],
                "pageInfo": {"isLastPage": True},
            },
        )

    transport = httpx.MockTransport(handler)
    docs = list(
        fetch_nocodb_records(
            table_ids="tbl_page",
            api_token="dummy_tok",
            page_size=1,
            transport=transport,
        )
    )
    assert len(docs) == 1
    assert call_count == 1
