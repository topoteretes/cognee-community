from __future__ import annotations

from pathlib import Path

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from cognee_community_connector_ashby.ashby import (
    ASHBY_TABLE_NAME,
    AshbyClient,
    ashby_source,
    fetch_ashby_jobs,
    get_retry_delay,
    job_to_document,
    strip_html,
)


def test_client_auth_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASHBY_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Ashby API key is required"):
        AshbyClient(api_key=None)


def test_client_headers_custom_base_url() -> None:
    client = AshbyClient(api_key="ashby_key_123", base_url="https://api.custom.ashbyhq.com/")
    assert client.base_url == "https://api.custom.ashbyhq.com"
    assert "Basic " in client.client.headers["Authorization"]
    client.close()


def test_get_retry_delay() -> None:
    resp_with_header = httpx.Response(429, headers={"Retry-After": "4.5"})
    assert get_retry_delay(resp_with_header, 0) == 4.5

    resp_no_header = httpx.Response(429)
    assert get_retry_delay(resp_no_header, 1, base_delay=1.0) == 2.0


def test_strip_html() -> None:
    raw = "<p>We are hiring!<br/>Come join our <b>AI</b> team.</p>"
    clean = strip_html(raw)
    assert "We are hiring!" in clean
    assert "Come join our AI team." in clean
    assert "<" not in clean


def test_client_retry_on_429() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json={"results": [{"id": "job_1", "title": "Staff Engineer"}]})

    transport = httpx.MockTransport(handler)
    with AshbyClient(api_key="test_key", transport=transport) as client:
        res = client.list_jobs()
        assert len(res["results"]) == 1
        assert call_count == 2


def test_client_network_error_retry() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.NetworkError("Reset connection")
        return httpx.Response(200, json={"results": [{"id": "job_2", "title": "Recovered"}]})

    transport = httpx.MockTransport(handler)
    with AshbyClient(api_key="test_key", transport=transport) as client:
        res = client.list_jobs()
        assert len(res["results"]) == 1
        assert call_count == 2


def test_client_list_jobs_params() -> None:
    captured_payload: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        import json

        captured_payload = json.loads(request.read())
        return httpx.Response(200, json={"results": []})

    transport = httpx.MockTransport(handler)
    with AshbyClient(api_key="test_key", transport=transport) as client:
        client.list_jobs(status="Open", cursor="cur_next")

    assert captured_payload is not None
    assert captured_payload.get("status") == "Open"
    assert captured_payload.get("cursor") == "cur_next"


def test_client_list_job_postings() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"id": "post_1", "title": "Post Title"}]},
        )

    transport = httpx.MockTransport(handler)
    with AshbyClient(api_key="test_key", transport=transport) as client:
        postings = client.list_job_postings(job_id="job_123")
        assert len(postings) == 1
        assert postings[0]["id"] == "post_1"


def test_job_to_document_formatting() -> None:
    raw_job = {
        "id": "job_ai_101",
        "title": "Senior AI Infrastructure Engineer",
        "status": "Open",
        "departmentName": "Core Engineering",
        "locationName": "San Francisco, CA",
        "employmentType": "Full-Time",
        "updatedAt": "2026-10-01T12:00:00Z",
    }
    raw_postings = [
        {
            "id": "post_101",
            "descriptionPlain": "Build high-throughput vector indexers and graph backends.",
        }
    ]

    doc = job_to_document(raw_job, job_postings=raw_postings)
    assert doc["id"] == "job_ai_101"
    assert doc["title"] == "Senior AI Infrastructure Engineer"
    assert doc["metadata"]["source"] == "ashby"
    assert doc["metadata"]["department"] == "Core Engineering"

    text = doc["text"]
    assert "# Role: Senior AI Infrastructure Engineer" in text
    assert "- **Job ID**: job_ai_101" in text
    assert "- **Status**: Open" in text
    assert "- **Department**: Core Engineering" in text
    assert "- **Location**: San Francisco, CA" in text
    assert "## Job Description & Requirements" in text
    assert "Build high-throughput vector indexers" in text


def test_job_to_document_fallback_description() -> None:
    raw_job = {
        "id": "job_blank",
        "title": "Product Designer",
    }
    doc = job_to_document(raw_job, job_postings=[])
    assert "*(No detailed description provided)*" in doc["text"]


def test_source_metadata_document_mode() -> None:
    source = ashby_source(api_key="dummy_token")
    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == "ashby"


def test_source_basic_ingestion(tmp_path: Path) -> None:
    mock_jobs = [
        {"id": "j1", "title": "Job One", "updatedAt": "2026-10-01T10:00:00Z"},
        {"id": "j2", "title": "Job Two", "updatedAt": "2026-10-02T10:00:00Z"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if "jobPosting.list" in request.url.path:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"results": mock_jobs})

    transport = httpx.MockTransport(handler)
    pipeline = dlt.pipeline(
        pipeline_name="test_ashby_pipeline",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )

    source = ashby_source(api_key="dummy_tok", transport=transport)
    info = pipeline.run(source)
    assert info.has_failed_jobs is False

    with pipeline.sql_client() as client:
        with client.execute_query(f"SELECT COUNT(*) FROM {ASHBY_TABLE_NAME}") as cursor:
            count = cursor.fetchone()[0]
            assert count == 2


def test_source_incremental_cursor_advancement(tmp_path: Path) -> None:
    batch1 = {"results": [{"id": "j10", "title": "Old Job", "updatedAt": "2026-10-01T12:00:00Z"}]}
    batch2 = {"results": [{"id": "j20", "title": "New Job", "updatedAt": "2026-10-06T15:00:00Z"}]}

    call_index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_index
        if "jobPosting.list" in request.url.path:
            return httpx.Response(200, json={"results": []})
        call_index += 1
        if call_index == 1:
            return httpx.Response(200, json=batch1)
        return httpx.Response(200, json=batch2)

    transport = httpx.MockTransport(handler)
    pipeline = dlt.pipeline(
        pipeline_name="test_ashby_incremental",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )

    # First run
    source1 = ashby_source(api_key="dummy_tok", incremental=True, transport=transport)
    pipeline.run(source1)

    # Second run
    source2 = ashby_source(api_key="dummy_tok", incremental=True, transport=transport)
    pipeline.run(source2)

    with pipeline.sql_client() as client:
        with client.execute_query(f"SELECT COUNT(*) FROM {ASHBY_TABLE_NAME}") as cursor:
            total = cursor.fetchone()[0]
            assert total == 2


def test_client_context_manager() -> None:
    with AshbyClient(api_key="dummy_tok") as client:
        assert client.client.is_closed is False
    assert client.client.is_closed is True


def test_source_skip_postings_flag() -> None:
    postings_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal postings_called
        if "jobPosting.list" in request.url.path:
            postings_called = True
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"results": [{"id": "j_skip", "title": "Skip Job"}]})

    transport = httpx.MockTransport(handler)
    docs = list(
        fetch_ashby_jobs(
            api_key="dummy_tok",
            include_job_postings=False,
            transport=transport,
        )
    )
    assert len(docs) == 1
    assert postings_called is False
