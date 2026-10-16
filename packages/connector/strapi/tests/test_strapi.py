from __future__ import annotations

from pathlib import Path

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from cognee_community_connector_strapi.strapi import (
    STRAPI_TABLE_NAME,
    StrapiClient,
    extract_entry_fields,
    fetch_strapi_entries,
    get_retry_delay,
    strapi_entry_to_document,
    strapi_source,
)


def test_client_auth_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRAPI_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Strapi API token is required"):
        StrapiClient(api_token=None)


def test_client_headers_custom_base_url() -> None:
    client = StrapiClient(api_token="token_abc", base_url="https://cms.company.com/")
    assert client.base_url == "https://cms.company.com"
    assert client.client.headers["Authorization"] == "Bearer token_abc"
    client.close()


def test_get_retry_delay() -> None:
    resp_with_header = httpx.Response(429, headers={"Retry-After": "4.2"})
    assert get_retry_delay(resp_with_header, 0) == 4.2

    assert get_retry_delay(httpx.Response(429, headers={"Retry-After": "0"}), 0) == 0.0
    assert get_retry_delay(httpx.Response(429, headers={"Retry-After": "0.5"}), 1) == 0.5

    resp_no_header = httpx.Response(429)
    assert get_retry_delay(resp_no_header, 1, base_delay=1.0) == 2.0


@pytest.mark.parametrize(
    "invalid_header",
    [
        "-1",
        "-10.5",
        "nan",
        "NaN",
        "inf",
        "-inf",
        "Infinity",
        "-Infinity",
        "invalid",
        "",
    ],
)
def test_get_retry_delay_invalid_fallback(invalid_header: str) -> None:
    """Fallback to exponential backoff when Retry-After is negative, NaN, infinity, or invalid."""
    resp = httpx.Response(429, headers={"Retry-After": invalid_header})
    assert get_retry_delay(resp, 0, base_delay=1.0) == 1.0
    assert get_retry_delay(resp, 2, base_delay=1.0) == 4.0


def test_client_retry_on_429() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json={"data": [{"id": 1, "attributes": {"title": "Article 1"}}]})

    transport = httpx.MockTransport(handler)
    with StrapiClient(api_token="test_tok", transport=transport) as client:
        res = client.list_entries(content_type="articles")
        assert len(res["data"]) == 1
        assert call_count == 2


def test_client_network_error_retry() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.NetworkError("Network interrupted")
        return httpx.Response(200, json={"data": [{"id": 2, "attributes": {"title": "Recovered"}}]})

    transport = httpx.MockTransport(handler)
    with StrapiClient(api_token="test_tok", transport=transport) as client:
        res = client.list_entries(content_type="articles")
        assert len(res["data"]) == 1
        assert call_count == 2


def test_client_list_entries_params() -> None:
    captured_url: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)
    with StrapiClient(api_token="test_tok", transport=transport) as client:
        client.list_entries(
            content_type="articles",
            page=3,
            page_size=25,
            updated_after="2026-10-01T00:00:00.000Z",
            publication_state="live",
        )

    assert captured_url is not None
    assert "/api/articles" in captured_url
    assert "page" in captured_url and "3" in captured_url
    assert "pageSize" in captured_url and "25" in captured_url
    assert "filters" in captured_url and "updatedAt" in captured_url


def test_extract_entry_fields_v4_nested() -> None:
    raw_v4 = {
        "id": 42,
        "attributes": {
            "title": "v4 Guide",
            "content": "Strapi v4 nested attributes content.",
            "publishedAt": "2026-10-05T12:00:00Z",
            "updatedAt": "2026-10-06T15:00:00Z",
            "author": {"data": {"attributes": {"name": "Jane Doe"}}},
            "category": {"data": {"attributes": {"name": "Engineering"}}},
        },
    }
    fields = extract_entry_fields(raw_v4)
    assert fields["id"] == "42"
    assert fields["title"] == "v4 Guide"
    assert fields["content"] == "Strapi v4 nested attributes content."
    assert fields["author_name"] == "Jane Doe"
    assert fields["category_name"] == "Engineering"


def test_extract_entry_fields_v5_flat() -> None:
    raw_v5 = {
        "documentId": "doc_999",
        "title": "v5 Flat Entry",
        "content": "Flat schema content in Strapi v5.",
        "publishedAt": "2026-10-07T10:00:00Z",
        "updatedAt": "2026-10-07T11:00:00Z",
        "author": {"name": "John Smith"},
        "category": {"name": "DevOps"},
    }
    fields = extract_entry_fields(raw_v5)
    assert fields["id"] == "doc_999"
    assert fields["title"] == "v5 Flat Entry"
    assert fields["content"] == "Flat schema content in Strapi v5."
    assert fields["author_name"] == "John Smith"
    assert fields["category_name"] == "DevOps"


def test_strapi_entry_to_document_formatting() -> None:
    raw_entry = {
        "id": 10,
        "attributes": {
            "title": "Knowledge Graph Tutorial",
            "content": "Step-by-step tutorial on Cognee memory.",
            "publishedAt": "2026-10-01T08:00:00Z",
            "updatedAt": "2026-10-02T09:00:00Z",
            "author": {"data": {"attributes": {"name": "Alice Author"}}},
            "category": {"data": {"attributes": {"name": "AI"}}},
        },
    }

    doc = strapi_entry_to_document(raw_entry, "tutorials")
    assert doc["id"] == "tutorials_10"
    assert doc["entry_id"] == "10"
    assert doc["content_type"] == "tutorials"
    assert doc["title"] == "Knowledge Graph Tutorial"
    assert doc["metadata"]["source"] == "strapi"
    assert doc["metadata"]["category"] == "AI"

    text = doc["text"]
    assert "# Knowledge Graph Tutorial" in text
    assert "- **Content Type**: tutorials" in text
    assert "- **Author**: Alice Author" in text
    assert "- **Category**: AI" in text
    assert "Step-by-step tutorial on Cognee memory." in text


def test_source_metadata_document_mode() -> None:
    source = strapi_source(api_token="dummy_token")
    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == "strapi"


def test_source_basic_ingestion(tmp_path: Path) -> None:
    mock_response = {
        "data": [
            {
                "id": 1,
                "attributes": {
                    "title": "Entry One",
                    "content": "First entry body.",
                    "updatedAt": "2026-10-01T10:00:00Z",
                },
            },
            {
                "id": 2,
                "attributes": {
                    "title": "Entry Two",
                    "content": "Second entry body.",
                    "updatedAt": "2026-10-02T10:00:00Z",
                },
            },
        ],
        "meta": {"pagination": {"page": 1, "pageSize": 100, "pageCount": 1}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_response)

    transport = httpx.MockTransport(handler)
    pipeline = dlt.pipeline(
        pipeline_name="test_strapi_pipeline",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )

    source = strapi_source(api_token="dummy_tok", transport=transport)
    info = pipeline.run(source)
    assert info.has_failed_jobs is False

    with pipeline.sql_client() as client:
        with client.execute_query(f"SELECT COUNT(*) FROM {STRAPI_TABLE_NAME}") as cursor:
            count = cursor.fetchone()[0]
            assert count == 2


def test_source_incremental_cursor_advancement(tmp_path: Path) -> None:
    batch1 = {
        "data": [
            {
                "id": 10,
                "attributes": {
                    "title": "Old Article",
                    "content": "Old content",
                    "updatedAt": "2026-10-01T12:00:00Z",
                },
            }
        ],
        "meta": {"pagination": {"page": 1, "pageSize": 100, "pageCount": 1}},
    }
    batch2 = {
        "data": [
            {
                "id": 20,
                "attributes": {
                    "title": "New Article",
                    "content": "New content",
                    "updatedAt": "2026-10-06T15:00:00Z",
                },
            }
        ],
        "meta": {"pagination": {"page": 1, "pageSize": 100, "pageCount": 1}},
    }

    last_filter_param: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal last_filter_param
        filter_val = request.url.params.get("filters[updatedAt][$gt]")
        last_filter_param = filter_val
        if filter_val == "2026-10-01T12:00:00Z":
            return httpx.Response(200, json=batch2)
        return httpx.Response(200, json=batch1)

    transport = httpx.MockTransport(handler)
    pipeline = dlt.pipeline(
        pipeline_name="test_strapi_incremental",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )

    # First run
    source1 = strapi_source(api_token="dummy_tok", incremental=True, transport=transport)
    pipeline.run(source1)

    # Second run
    source2 = strapi_source(api_token="dummy_tok", incremental=True, transport=transport)
    pipeline.run(source2)

    assert last_filter_param == "2026-10-01T12:00:00Z"

    with pipeline.sql_client() as client:
        with client.execute_query(f"SELECT COUNT(*) FROM {STRAPI_TABLE_NAME}") as cursor:
            total = cursor.fetchone()[0]
            assert total == 2


def test_source_multiple_content_types() -> None:
    requested_types: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_types.append(request.url.path)
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)
    docs = list(
        fetch_strapi_entries(
            content_types=["articles", "docs"],
            api_token="dummy_tok",
            transport=transport,
        )
    )
    assert docs == []
    assert any("/api/articles" in p for p in requested_types)
    assert any("/api/docs" in p for p in requested_types)


def test_client_context_manager() -> None:
    with StrapiClient(api_token="dummy_tok") as client:
        assert client.client.is_closed is False
    assert client.client.is_closed is True


def test_client_server_error_retry_exhaustion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(handler)
    with StrapiClient(api_token="dummy_tok", max_retries=1, transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.list_entries(content_type="articles")
