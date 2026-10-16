"""Comprehensive unit tests for the Google Search Console connector."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from cognee_community_connector_google_search_console import (
    GoogleSearchConsoleClient,
    google_search_console_source,
)
from cognee_community_connector_google_search_console.google_search_console import (
    GSC_SOURCE_NAME,
    GSC_TABLE_NAME,
    _get_retry_delay,
    _row_to_document,
)

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"


# ---------------------------------------------------------------------------
# Client & Auth Tests
# ---------------------------------------------------------------------------
def test_client_auth_direct_token() -> None:
    client = GoogleSearchConsoleClient(token="test_bearer_token")
    headers = client.get_auth_header()
    assert headers["Authorization"] == "Bearer test_bearer_token"
    assert headers["Accept"] == "application/json"


def test_client_missing_auth_raises() -> None:
    client = GoogleSearchConsoleClient(token=None, refresh_token=None)
    with pytest.raises(ValueError, match="Google Search Console authentication requires"):
        client.get_auth_header()


def test_client_token_refresh() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "oauth2.googleapis.com/token" in str(request.url):
            return httpx.Response(200, json={"access_token": "refreshed_access_token_123"})
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleSearchConsoleClient(
        client_id="my_client_id",
        client_secret="my_client_secret",
        refresh_token="my_refresh_token",
        http_client=mock_client,
    )

    new_token = client.refresh_access_token()
    assert new_token == "refreshed_access_token_123"
    assert client.get_auth_header()["Authorization"] == "Bearer refreshed_access_token_123"
    assert len(calls) == 1


def test_client_load_credentials_files(tmp_path: Any) -> None:
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "file_client_id",
                    "client_secret": "file_client_secret",
                }
            }
        )
    )

    token_file = tmp_path / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "file_access_token",
                "refresh_token": "file_refresh_token",
            }
        )
    )

    client = GoogleSearchConsoleClient(
        credentials_path=str(creds_file),
        token_path=str(token_file),
    )
    assert client._client_id == "file_client_id"
    assert client._client_secret == "file_client_secret"
    assert client._token == "file_access_token"
    assert client._refresh_token == "file_refresh_token"


def test_client_list_sites() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "/sites" in str(request.url)
        assert request.headers["Authorization"] == "Bearer test_token"
        return httpx.Response(
            200,
            json={
                "siteEntry": [
                    {"siteUrl": "https://example.com/", "permissionLevel": "siteOwner"},
                    {"siteUrl": "sc-domain:example.org", "permissionLevel": "siteFullUser"},
                ]
            },
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleSearchConsoleClient(token="test_token", http_client=mock_client)
    sites = client.list_sites()
    assert len(sites) == 2
    assert sites[0]["siteUrl"] == "https://example.com/"
    assert sites[1]["permissionLevel"] == "siteFullUser"


def test_client_query_search_analytics() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "sites/https%3A%2F%2Fexample.com%2F/searchAnalytics/query" in str(request.url)
        body = json.loads(request.content.decode("utf-8"))
        assert body["startDate"] == "2026-09-01"
        assert body["endDate"] == "2026-09-28"
        assert body["dimensions"] == ["query", "page"]
        assert body["startRow"] == 0
        assert body["rowLimit"] == 500

        return httpx.Response(
            200,
            json={
                "rows": [
                    {
                        "keys": ["cognee graph rag", "https://example.com/docs/rag"],
                        "clicks": 140,
                        "impressions": 2500,
                        "ctr": 0.056,
                        "position": 2.4,
                    }
                ],
                "responseAggregationType": "byPage",
            },
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleSearchConsoleClient(token="test_token", http_client=mock_client)
    result = client.query_search_analytics(
        site_url="https://example.com/",
        start_date="2026-09-01",
        end_date="2026-09-28",
        dimensions=["query", "page"],
        row_limit=500,
        start_row=0,
    )

    rows = result.get("rows", [])
    assert len(rows) == 1
    assert rows[0]["clicks"] == 140


def test_client_rate_limit_and_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    attempt_count = 0

    def mock_sleep(_delay: float) -> None:
        pass

    monkeypatch.setattr("time.sleep", mock_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"siteEntry": []})

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleSearchConsoleClient(token="test_token", http_client=mock_client)
    sites = client.list_sites()
    assert sites == []
    assert attempt_count == 3


def test_get_retry_delay() -> None:
    assert _get_retry_delay({"retry-after": "5"}, 0) == 5.0
    assert _get_retry_delay({"retry-after": "0"}, 0) == 0.0
    assert _get_retry_delay({"retry-after": "0.5"}, 1) == 0.5
    assert _get_retry_delay({}, 2) == 4.0


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
    assert _get_retry_delay({"retry-after": invalid_header}, 0) == 1.0
    assert _get_retry_delay({"retry-after": invalid_header}, 2) == 4.0


# ---------------------------------------------------------------------------
# Document Formatting Tests
# ---------------------------------------------------------------------------
def test_row_to_document_formatting() -> None:
    row = {
        "keys": ["graph rag tutorial", "https://example.com/blog/tutorial"],
        "clicks": 52,
        "impressions": 1050,
        "ctr": 0.0495,
        "position": 4.2,
    }
    doc = _row_to_document(
        row=row,
        site_url="https://example.com/",
        dimensions=["query", "page"],
        start_date="2026-09-01",
        end_date="2026-09-28",
    )

    assert (
        doc["id"] == "gsc:https://example.com/:graph rag tutorial:https://example.com/blog/tutorial"
    )
    assert doc["query"] == "graph rag tutorial"
    assert doc["page"] == "https://example.com/blog/tutorial"
    assert doc["clicks"] == 52
    assert doc["impressions"] == 1050
    assert doc["ctr"] == 0.0495
    assert doc["position"] == 4.2
    assert doc["url"] == "https://example.com/blog/tutorial"
    assert 'Google Search Console Performance: "graph rag tutorial"' in doc["content"]
    assert "During the period 2026-09-01 to 2026-09-28" in doc["content"]
    assert doc["_deleted"] is False


def test_row_to_document_multi_dimensions() -> None:
    row = {
        "keys": ["search ai", "https://example.com/ai", "usa", "mobile"],
        "clicks": 10,
        "impressions": 200,
        "ctr": 0.05,
        "position": 3.0,
    }
    doc = _row_to_document(
        row=row,
        site_url="https://example.com/",
        dimensions=["query", "page", "country", "device"],
        start_date="2026-09-01",
        end_date="2026-09-28",
    )
    assert doc["id"] == "gsc:https://example.com/:search ai:https://example.com/ai:usa:mobile"
    assert "- **Country**: USA" in doc["content"]
    assert "- **Device**: Mobile" in doc["content"]


# ---------------------------------------------------------------------------
# DLT Source Ingestion & Pagination Tests
# ---------------------------------------------------------------------------
def test_source_basic_ingestion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/sites" in url_str and "searchAnalytics" not in url_str:
            return httpx.Response(
                200,
                json={
                    "siteEntry": [
                        {"siteUrl": "https://example.com/", "permissionLevel": "siteOwner"}
                    ]
                },
            )
        if "searchAnalytics/query" in url_str:
            return httpx.Response(
                200,
                json={
                    "rows": [
                        {
                            "keys": ["top query 1", "https://example.com/p1"],
                            "clicks": 100,
                            "impressions": 1000,
                            "ctr": 0.10,
                            "position": 1.5,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleSearchConsoleClient(token="test_token", http_client=mock_client)

    source = google_search_console_source(
        client=client,
        start_date="2026-09-01",
        end_date="2026-09-28",
    )

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == GSC_SOURCE_NAME
    resource = source.resources[GSC_TABLE_NAME]
    assert resource.write_disposition == "replace"

    rows = list(resource)
    assert len(rows) == 1
    assert rows[0]["query"] == "top query 1"
    assert rows[0]["clicks"] == 100
    assert rows[0]["site_url"] == "https://example.com/"


def test_source_site_urls_filter() -> None:
    queried_sites = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "searchAnalytics/query" in url_str:
            site = unquote(url_str.split("/sites/")[1].split("/searchAnalytics")[0])
            queried_sites.append(site)
            return httpx.Response(200, json={"rows": []})
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleSearchConsoleClient(token="test_token", http_client=mock_client)

    source = google_search_console_source(
        client=client,
        site_urls=["https://custom-site.com/"],
        start_date="2026-09-01",
        end_date="2026-09-28",
    )

    list(source.resources[GSC_TABLE_NAME])
    assert queried_sites == ["https://custom-site.com/"]


def test_source_pagination() -> None:
    page_offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        offset = body.get("startRow", 0)
        page_offsets.append(offset)

        if offset == 0:
            # First page returns 2 items (equal to row_limit=2) -> triggers next page
            return httpx.Response(
                200,
                json={
                    "rows": [
                        {
                            "keys": ["query 1", "https://example.com/1"],
                            "clicks": 1,
                            "impressions": 10,
                        },
                        {
                            "keys": ["query 2", "https://example.com/2"],
                            "clicks": 2,
                            "impressions": 20,
                        },
                    ]
                },
            )
        else:
            # Second page returns 1 item (< row_limit=2) -> terminates pagination
            return httpx.Response(
                200,
                json={
                    "rows": [
                        {
                            "keys": ["query 3", "https://example.com/3"],
                            "clicks": 3,
                            "impressions": 30,
                        },
                    ]
                },
            )

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleSearchConsoleClient(token="test_token", http_client=mock_client)

    source = google_search_console_source(
        client=client,
        site_urls=["https://example.com/"],
        row_limit=2,
        start_date="2026-09-01",
        end_date="2026-09-28",
    )

    rows = list(source.resources[GSC_TABLE_NAME])
    assert len(rows) == 3
    assert page_offsets == [0, 2]
    assert [r["query"] for r in rows] == ["query 1", "query 2", "query 3"]


def test_source_skip_unverified_403_site() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "forbidden-site" in url_str:
            return httpx.Response(403, json={"error": {"message": "User does not have permission"}})
        if "allowed-site" in url_str:
            return httpx.Response(
                200,
                json={
                    "rows": [{"keys": ["allowed query", "https://allowed-site.com/"], "clicks": 5}]
                },
            )
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleSearchConsoleClient(token="test_token", http_client=mock_client)

    source = google_search_console_source(
        client=client,
        site_urls=["https://forbidden-site.com/", "https://allowed-site.com/"],
        start_date="2026-09-01",
        end_date="2026-09-28",
    )

    rows = list(source.resources[GSC_TABLE_NAME])
    assert len(rows) == 1
    assert rows[0]["query"] == "allowed query"


def test_source_forget_on_delete_merge_tombstone(tmp_path: Any) -> None:
    import dlt

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rows": []})

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GoogleSearchConsoleClient(token="test_token", http_client=mock_client)

    db_path = tmp_path / "gsc_test.db"
    pipelines_dir = str(tmp_path / "dlt_pipelines")

    # First run on a dlt pipeline to populate resource state
    pipeline = dlt.pipeline(
        pipeline_name="test_gsc_merge_pipeline",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="gsc_test",
        pipelines_dir=pipelines_dir,
    )

    source1 = google_search_console_source(
        client=client,
        site_urls=["https://keep-site.com/", "https://deleted-site.com/"],
        write_disposition="merge",
        start_date="2026-09-01",
        end_date="2026-09-28",
    )
    pipeline.run(source1)

    # Second run without deleted-site.com
    source2 = google_search_console_source(
        client=client,
        site_urls=["https://keep-site.com/"],
        write_disposition="merge",
        start_date="2026-09-01",
        end_date="2026-09-28",
    )

    load_info = pipeline.run(source2)
    assert load_info.has_failed_jobs is False
