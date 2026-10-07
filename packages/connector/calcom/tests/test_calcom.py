from __future__ import annotations

from pathlib import Path

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from cognee_community_connector_calcom.calcom import (
    CALCOM_TABLE_NAME,
    CalComClient,
    booking_to_document,
    calcom_source,
    fetch_calcom_bookings,
    get_retry_delay,
)


def test_client_auth_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CALCOM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Cal.com API key is required"):
        CalComClient(api_key=None)


def test_client_headers_custom_base_url() -> None:
    client = CalComClient(api_key="cal_secret_123", base_url="https://custom.cal.com/v1/")
    assert client.base_url == "https://custom.cal.com/v1"
    assert client.client.headers["Authorization"] == "Bearer cal_secret_123"
    client.close()


def test_get_retry_delay() -> None:
    resp_with_header = httpx.Response(429, headers={"Retry-After": "5.5"})
    assert get_retry_delay(resp_with_header, 0) == 5.5

    resp_no_header = httpx.Response(429)
    assert get_retry_delay(resp_no_header, 2, base_delay=1.0) == 4.0


def test_client_retry_on_429() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json={"bookings": [{"id": 101, "title": "Test Sync"}]})

    transport = httpx.MockTransport(handler)
    with CalComClient(api_key="cal_test_key", transport=transport) as client:
        bookings = client.list_bookings()
        assert len(bookings) == 1
        assert bookings[0]["id"] == 101
        assert call_count == 2


def test_client_network_error_retry() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.NetworkError("Temporary network reset")
        return httpx.Response(200, json={"bookings": [{"id": 202, "title": "Recovered"}]})

    transport = httpx.MockTransport(handler)
    with CalComClient(api_key="cal_test_key", transport=transport) as client:
        bookings = client.list_bookings()
        assert len(bookings) == 1
        assert bookings[0]["id"] == 202
        assert call_count == 2


def test_client_list_bookings_params() -> None:
    captured_url: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(200, json={"bookings": []})

    transport = httpx.MockTransport(handler)
    with CalComClient(api_key="cal_test_key", transport=transport) as client:
        client.list_bookings(status="ACCEPTED", after="2026-10-01T00:00:00Z", page=2, take=50)

    assert captured_url is not None
    assert "status=ACCEPTED" in captured_url
    assert "page=2" in captured_url
    assert "take=50" in captured_url
    assert "after=2026-10-01T00%3A00%3A00Z" in captured_url


def test_booking_to_document_formatting() -> None:
    raw_booking = {
        "id": 1234,
        "title": "Architecture Review",
        "description": "Discussing Cognee knowledge graph memory layer.",
        "status": "ACCEPTED",
        "startTime": "2026-10-07T14:00:00Z",
        "endTime": "2026-10-07T15:00:00Z",
        "location": "https://meet.google.com/xyz-abc",
        "user": {"name": "Alice Dev", "email": "alice@example.com"},
        "eventType": {"title": "Tech Sync"},
        "attendees": [
            {"name": "Bob Builder", "email": "bob@example.com", "timeZone": "America/New_York"}
        ],
        "responses": {
            "Project Scope": "Implementing Cal.com connector",
            "Target Date": "Mergetober",
        },
    }

    doc = booking_to_document(raw_booking)

    assert doc["id"] == "1234"
    assert doc["title"] == "Architecture Review"
    assert doc["status"] == "ACCEPTED"
    assert doc["metadata"]["source"] == "calcom"
    assert doc["metadata"]["event_type"] == "Tech Sync"

    text = doc["text"]
    assert "# Meeting: Architecture Review" in text
    assert "- **Host**: Alice Dev (alice@example.com)" in text
    assert "- **Location**: https://meet.google.com/xyz-abc" in text
    assert "## Description & Agenda" in text
    assert "Cognee knowledge graph" in text
    assert "Bob Builder (bob@example.com) [America/New_York]" in text
    assert "- **Project Scope**: Implementing Cal.com connector" in text


def test_booking_to_document_responses_list() -> None:
    raw_booking = {
        "id": 5678,
        "title": "Onboarding Call",
        "responses": [
            {"label": "Team Size", "value": "15 engineers"},
            {"label": "Primary Cloud", "value": "AWS"},
        ],
    }

    doc = booking_to_document(raw_booking)
    text = doc["text"]
    assert "- **Team Size**: 15 engineers" in text
    assert "- **Primary Cloud**: AWS" in text


def test_booking_to_document_cancellation_reason() -> None:
    raw_booking = {
        "id": 9999,
        "title": "Cancelled Meeting",
        "status": "CANCELLED",
        "cancellationReason": "Conflict with company all-hands",
    }

    doc = booking_to_document(raw_booking)
    assert "- **Status**: CANCELLED" in doc["text"]
    assert "## Cancellation Reason" in doc["text"]
    assert "Conflict with company all-hands" in doc["text"]


def test_source_metadata_document_mode() -> None:
    source = calcom_source(api_key="cal_dummy")
    assert getattr(source, DOCUMENT_SOURCE_ATTR, None) == "calcom"


def test_source_basic_ingestion(tmp_path: Path) -> None:
    mock_bookings = [
        {
            "id": 1,
            "title": "First Meeting",
            "startTime": "2026-10-01T10:00:00Z",
            "updatedAt": "2026-10-01T10:00:00Z",
            "status": "ACCEPTED",
        },
        {
            "id": 2,
            "title": "Second Meeting",
            "startTime": "2026-10-02T11:00:00Z",
            "updatedAt": "2026-10-02T11:00:00Z",
            "status": "ACCEPTED",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"bookings": mock_bookings})

    transport = httpx.MockTransport(handler)
    pipeline = dlt.pipeline(
        pipeline_name="test_calcom_pipeline",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )

    source = calcom_source(api_key="cal_dummy_key", transport=transport)
    info = pipeline.run(source)
    assert info.has_failed_jobs is False

    with pipeline.sql_client() as client:
        with client.execute_query(f"SELECT COUNT(*) FROM {CALCOM_TABLE_NAME}") as cursor:
            count = cursor.fetchone()[0]
            assert count == 2


def test_source_incremental_cursor_advancement(tmp_path: Path) -> None:
    batch1 = [
        {
            "id": 10,
            "title": "Meeting 1",
            "updatedAt": "2026-10-01T12:00:00Z",
            "status": "ACCEPTED",
        }
    ]
    batch2 = [
        {
            "id": 20,
            "title": "Meeting 2",
            "updatedAt": "2026-10-05T15:00:00Z",
            "status": "ACCEPTED",
        }
    ]

    last_after_param: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal last_after_param
        after = request.url.params.get("after")
        last_after_param = after
        if after == "2026-10-01T12:00:00Z":
            return httpx.Response(200, json={"bookings": batch2})
        return httpx.Response(200, json={"bookings": batch1})

    transport = httpx.MockTransport(handler)
    pipeline = dlt.pipeline(
        pipeline_name="test_calcom_incremental",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )

    # First run
    source1 = calcom_source(api_key="cal_dummy_key", incremental=True, transport=transport)
    pipeline.run(source1)

    # Second run
    source2 = calcom_source(api_key="cal_dummy_key", incremental=True, transport=transport)
    pipeline.run(source2)

    assert last_after_param == "2026-10-01T12:00:00Z"

    with pipeline.sql_client() as client:
        with client.execute_query(f"SELECT COUNT(*) FROM {CALCOM_TABLE_NAME}") as cursor:
            total_records = cursor.fetchone()[0]
            assert total_records == 2


def test_source_status_filter() -> None:
    requested_status: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested_status
        requested_status = request.url.params.get("status")
        return httpx.Response(200, json={"bookings": []})

    transport = httpx.MockTransport(handler)
    docs = list(
        fetch_calcom_bookings(
            api_key="cal_dummy_key",
            status_filter="CANCELLED",
            transport=transport,
        )
    )
    assert docs == []
    assert requested_status == "CANCELLED"


def test_client_context_manager() -> None:
    with CalComClient(api_key="cal_dummy_key") as client:
        assert client.client.is_closed is False
    assert client.client.is_closed is True


def test_client_list_bookings_data_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": 303, "title": "Data key booking"}]})

    transport = httpx.MockTransport(handler)
    with CalComClient(api_key="cal_dummy_key", transport=transport) as client:
        bookings = client.list_bookings()
        assert len(bookings) == 1
        assert bookings[0]["id"] == 303
