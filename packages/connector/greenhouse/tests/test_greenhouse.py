"""Comprehensive unit tests for the Greenhouse data-source connector."""

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_greenhouse.greenhouse import (
    GREENHOUSE_SOURCE_NAME,
    GREENHOUSE_TABLE_NAME,
    GreenhouseClient,
    _get_retry_delay,
    _job_to_document,
    _scorecard_to_document,
    _strip_html,
    greenhouse_source,
)


def test_client_auth_missing_raises(monkeypatch):
    """Client raises ValueError when no Harvest API key is provided or in environment."""
    monkeypatch.delenv("GREENHOUSE_HARVEST_API_KEY", raising=False)
    monkeypatch.delenv("GREENHOUSE_API_KEY", raising=False)
    monkeypatch.delenv("GREENHOUSE_ACCESS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Greenhouse Harvest API key required"):
        GreenhouseClient()


def test_client_headers_basic_auth():
    """Client configures Basic auth and custom user agent."""
    client = GreenhouseClient(api_key="harvest_key_xyz")
    auth_header = client.client.headers["authorization"]
    assert auth_header.startswith("Basic ")
    assert "cognee-community-connector-greenhouse" in client.client.headers["user-agent"]
    client.close()


def test_strip_html():
    """HTML tags are cleanly stripped from job description content."""
    html = "<p>We are looking for a <strong>Senior Engineer</strong> to lead.</p>"
    assert _strip_html(html) == "We are looking for a Senior Engineer to lead."


def test_get_retry_delay():
    """Retry delay honors Retry-After header and falls back to backoff."""
    req = httpx.Request("GET", "https://harvest.greenhouse.io/v1/test")
    resp_with_header = httpx.Response(429, headers={"retry-after": "5"}, request=req)
    assert _get_retry_delay(resp_with_header, 0) == 5.0
    assert (
        _get_retry_delay(httpx.Response(429, headers={"retry-after": "0"}, request=req), 0) == 0.0
    )
    assert (
        _get_retry_delay(httpx.Response(429, headers={"retry-after": "0.5"}, request=req), 1) == 0.5
    )

    resp_without_header = httpx.Response(500, request=req)
    assert _get_retry_delay(resp_without_header, 2) == 4.0
    assert _get_retry_delay(None, 1) == 2.0


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
    req = httpx.Request("GET", "https://harvest.greenhouse.io/v1/test")
    resp = httpx.Response(429, headers={"retry-after": invalid_header}, request=req)
    assert _get_retry_delay(resp, 0) == 1.0
    assert _get_retry_delay(resp, 2) == 4.0


def test_client_retry_on_429(monkeypatch):
    """Client retries on HTTP 429 and succeeds."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    attempts = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "0.1"}, request=request)
        return httpx.Response(200, json=[{"id": 1, "name": "Staff AI Engineer"}], request=request)

    transport = httpx.MockTransport(mock_handler)
    client = GreenhouseClient(api_key="tok", transport=transport)
    res = client.list_jobs()
    assert attempts == 2
    assert len(res) == 1
    assert res[0]["id"] == 1
    client.close()


def test_client_network_error_retry(monkeypatch):
    """Client retries on network error and succeeds."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    attempts = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.NetworkError("Greenhouse socket reset")
        return httpx.Response(200, json=[], request=request)

    transport = httpx.MockTransport(mock_handler)
    client = GreenhouseClient(api_key="tok", transport=transport)
    res = client.list_jobs()
    assert attempts == 2
    assert res == []
    client.close()


def test_client_list_jobs_params():
    """Client formats query parameters for /jobs."""
    captured_url = None

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(200, json=[], request=request)

    transport = httpx.MockTransport(mock_handler)
    client = GreenhouseClient(api_key="tok", transport=transport)
    client.list_jobs(
        updated_after="2026-10-01T00:00:00Z",
        status="open",
        page=2,
        per_page=50,
    )

    assert "updated_after=2026-10-01T00%3A00%3A00Z" in captured_url
    assert "status=open" in captured_url
    assert "page=2" in captured_url
    assert "per_page=50" in captured_url
    client.close()


def test_client_list_job_posts():
    """Client queries job posts for a specific job ID."""
    captured_url = None

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(200, json=[{"id": 10, "title": "Job Post Title"}], request=request)

    transport = httpx.MockTransport(mock_handler)
    client = GreenhouseClient(api_key="tok", transport=transport)
    posts = client.list_job_posts(job_id=456)

    assert "/jobs/456/job_posts" in captured_url
    assert len(posts) == 1
    client.close()


def test_client_list_scorecards():
    """Client queries scorecards with job and updated_after filters."""
    captured_url = None

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(200, json=[{"id": 99}], request=request)

    transport = httpx.MockTransport(mock_handler)
    client = GreenhouseClient(api_key="tok", transport=transport)
    scs = client.list_scorecards(job_id=123, updated_after="2026-10-02T00:00:00Z")

    assert "/scorecards" in captured_url
    assert "job_id=123" in captured_url
    assert "updated_after=2026-10-02T00%3A00%3A00Z" in captured_url
    assert len(scs) == 1
    client.close()


def test_job_to_document_formatting():
    """Job document formats title, departments, notes, and post descriptions."""
    job = {
        "id": 1001,
        "name": "Senior Backend Engineer",
        "requisition_id": "REQ-2026-08",
        "status": "open",
        "departments": [{"name": "Platform Engineering"}],
        "offices": [{"name": "San Francisco, CA"}],
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-10-01T12:00:00Z",
        "notes": "Looking for high distributed systems and knowledge graph experience.",
    }
    posts = [
        {
            "id": 2001,
            "title": "Senior Backend Engineer - US",
            "content": "<p>Build scalable pipelines with Python, DLT, and Cognee.</p>",
        }
    ]

    doc = _job_to_document(job, posts)
    assert doc["id"] == "greenhouse:job:1001"
    assert doc["data_id"].startswith("greenhouse:")
    assert doc["name"] == "Job: Senior Backend Engineer (Platform Engineering)"
    assert doc["external_metadata"]["source"] == GREENHOUSE_SOURCE_NAME

    text = doc["text"]
    assert "# Greenhouse Job: Senior Backend Engineer" in text
    assert "REQ-2026-08" in text
    assert "Platform Engineering" in text
    assert "high distributed systems and knowledge graph experience" in text
    assert "Build scalable pipelines with Python, DLT, and Cognee." in text


def test_scorecard_to_document_formatting():
    """Scorecard document formats evaluation criteria and omits candidate PII."""
    scorecard = {
        "id": 8888,
        "interview": "System Design Architecture",
        "overall_recommendation": "definitely_yes",
        "submitted_at": "2026-10-03T16:00:00Z",
        "ratings": {"Architecture Scalability": "Strong Yes", "Data Modeling": "Yes"},
        "questions": [
            {
                "question": "How did the candidate design vector search and graph hybrid storage?",
                "answer": "Strong separation of ingestion staging and vector embeddings.",
            }
        ],
    }

    doc = _scorecard_to_document(scorecard)
    assert doc["id"] == "greenhouse:scorecard:8888"
    assert doc["type"] == "scorecard"
    assert doc["recommendation"] == "definitely_yes"

    text = doc["text"]
    assert "# Greenhouse Scorecard: System Design Architecture" in text
    assert "Architecture Scalability**: Strong Yes" in text
    assert "How did the candidate design vector search and graph hybrid storage?" in text
    assert "Strong separation of ingestion staging and vector embeddings." in text


def test_source_metadata_document_mode():
    """greenhouse_source declares cognee_document_source and DOCUMENT_SOURCE_ATTR."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], request=request)

    client = GreenhouseClient(api_key="tok", transport=httpx.MockTransport(mock_handler))
    source = greenhouse_source(client=client)

    assert source.cognee_document_source == "greenhouse"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "greenhouse"
    assert GREENHOUSE_TABLE_NAME in source.resources
    assert source.resources[GREENHOUSE_TABLE_NAME].write_disposition == "replace"
    client.close()


def test_source_basic_ingestion(tmp_path):
    """Source executes through dlt pipeline and yields jobs documents."""
    jobs_payload = [
        {
            "id": 501,
            "name": "Frontend Lead",
            "status": "open",
            "departments": [{"name": "UI Engineering"}],
            "updated_at": "2026-10-01T10:00:00Z",
        }
    ]

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/jobs/501/job_posts" in url:
            return httpx.Response(200, json=[], request=request)
        if "/jobs" in url:
            return httpx.Response(200, json=jobs_payload, request=request)
        return httpx.Response(404, request=request)

    client = GreenhouseClient(api_key="tok", transport=httpx.MockTransport(mock_handler))
    source = greenhouse_source(client=client)

    pipeline = dlt.pipeline(
        pipeline_name="test_gh_pipe",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )
    load_info = pipeline.run(source)
    assert load_info.has_failed_jobs is False

    items = list(source.resources[GREENHOUSE_TABLE_NAME]())
    assert len(items) == 1
    assert items[0]["name"] == "Job: Frontend Lead (UI Engineering)"
    client.close()


def test_source_incremental_cursor_advancement(tmp_path):
    """Source advances last_updated_after watermark cursor in dlt state."""
    jobs_payload = [
        {"id": 1, "name": "J1", "updated_at": "2026-10-01T00:00:00Z"},
        {"id": 2, "name": "J2", "updated_at": "2026-10-06T15:00:00Z"},
    ]

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/job_posts" in url:
            return httpx.Response(200, json=[], request=request)
        if "/jobs" in url:
            return httpx.Response(200, json=jobs_payload, request=request)
        return httpx.Response(404, request=request)

    client = GreenhouseClient(api_key="tok", transport=httpx.MockTransport(mock_handler))
    source = greenhouse_source(client=client)

    pipeline = dlt.pipeline(
        pipeline_name="test_gh_cursor_pipe",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )
    pipeline.run(source)

    state = pipeline.state.get("sources", {}).get(GREENHOUSE_SOURCE_NAME, {})
    resource_state = state.get("resources", {}).get(GREENHOUSE_TABLE_NAME, {})
    assert resource_state.get("last_updated_after") == "2026-10-06T15:00:00Z"
    client.close()


def test_source_interview_feedback_opt_in_gate():
    """Scorecards are skipped by default, and fetched only when include_interview_feedback=True."""
    scorecards_called = False

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal scorecards_called
        url = str(request.url)
        if "/scorecards" in url:
            scorecards_called = True
            return httpx.Response(
                200, json=[{"id": 901, "interview": "Tech Assessment"}], request=request
            )
        if "/job_posts" in url:
            return httpx.Response(200, json=[], request=request)
        if "/jobs" in url:
            return httpx.Response(200, json=[{"id": 1, "name": "Engineer"}], request=request)
        return httpx.Response(404, request=request)

    client = GreenhouseClient(api_key="tok", transport=httpx.MockTransport(mock_handler))

    # 1. Default: include_interview_feedback=False
    source_default = greenhouse_source(client=client)
    items_default = list(source_default.resources[GREENHOUSE_TABLE_NAME]())
    assert scorecards_called is False
    assert len(items_default) == 1

    # 2. Opt-in: include_interview_feedback=True
    source_opt_in = greenhouse_source(include_interview_feedback=True, client=client)
    items_opt_in = list(source_opt_in.resources[GREENHOUSE_TABLE_NAME]())
    assert scorecards_called is True
    assert len(items_opt_in) == 2
    types = [i["type"] for i in items_opt_in]
    assert "job" in types
    assert "scorecard" in types
    client.close()
