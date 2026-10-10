"""Comprehensive unit tests for the Calendly data-source connector."""

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_calendly.calendly import (
    CALENDLY_SOURCE_NAME,
    CALENDLY_TABLE_NAME,
    CalendlyClient,
    _event_to_document,
    _extract_uuid_from_uri,
    _get_retry_delay,
    calendly_source,
)


def test_client_auth_missing_raises(monkeypatch):
    """Client raises ValueError when no token is provided or set in environment."""
    monkeypatch.delenv("CALENDLY_API_KEY", raising=False)
    monkeypatch.delenv("CALENDLY_ACCESS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="Calendly API key required"):
        CalendlyClient()


def test_client_headers_and_auth():
    """Client configures Bearer token and custom user agent."""
    client = CalendlyClient(api_key="test_cal_pat_12345")
    assert client.client.headers["authorization"] == "Bearer test_cal_pat_12345"
    assert "cognee-community-connector-calendly" in client.client.headers["user-agent"]
    client.close()


def test_extract_uuid_from_uri():
    """Extracts standard UUIDs from Calendly URIs."""
    uri = "https://api.calendly.com/scheduled_events/12345678-1234-5678-1234-567812345678"
    assert _extract_uuid_from_uri(uri) == "12345678-1234-5678-1234-567812345678"
    assert _extract_uuid_from_uri("custom_id_999") == "custom_id_999"


def test_get_retry_delay():
    """Retry delay honors Retry-After header and falls back to backoff."""
    req = httpx.Request("GET", "https://api.calendly.com/test")
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
    req = httpx.Request("GET", "https://api.calendly.com/test")
    resp = httpx.Response(429, headers={"retry-after": invalid_header}, request=req)
    assert _get_retry_delay(resp, 0) == 1.0
    assert _get_retry_delay(resp, 2) == 4.0


def test_client_retry_on_429(monkeypatch):
    """Client retries on HTTP 429 and returns successful payload."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    attempts = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "0.1"}, request=request)
        return httpx.Response(
            200,
            json={"resource": {"uri": "https://api.calendly.com/users/USER_1"}},
            request=request,
        )

    transport = httpx.MockTransport(mock_handler)
    client = CalendlyClient(api_key="mock_pat", transport=transport)
    user = client.get_current_user()
    assert attempts == 2
    assert user["uri"] == "https://api.calendly.com/users/USER_1"
    client.close()


def test_client_list_scheduled_events():
    """Client queries scheduled events with expected query parameters."""
    captured_url = None

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_url
        captured_url = str(request.url)
        return httpx.Response(
            200,
            json={
                "collection": [
                    {
                        "uri": "https://api.calendly.com/scheduled_events/evt_1",
                        "name": "Sprint Planning",
                        "status": "active",
                    }
                ],
                "pagination": {"next_page_token": None},
            },
            request=request,
        )

    transport = httpx.MockTransport(mock_handler)
    client = CalendlyClient(api_key="mock_pat", transport=transport)
    data = client.list_scheduled_events(
        user_uri="https://api.calendly.com/users/U1",
        min_start_time="2026-10-01T00:00:00Z",
        status="active",
        count=50,
    )

    assert "user=https%3A%2F%2Fapi.calendly.com%2Fusers%2FU1" in captured_url
    assert "min_start_time=2026-10-01T00%3A00%3A00Z" in captured_url
    assert "status=active" in captured_url
    assert "count=50" in captured_url
    assert len(data["collection"]) == 1
    client.close()


def test_client_list_event_invitees_pagination():
    """Client paginates through all pages of invitees."""
    page = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal page
        page += 1
        if page == 1:
            return httpx.Response(
                200,
                json={
                    "collection": [{"name": "Invitee 1", "email": "inv1@test.com"}],
                    "pagination": {"next_page_token": "token_page_2"},
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "collection": [{"name": "Invitee 2", "email": "inv2@test.com"}],
                "pagination": {"next_page_token": None},
            },
            request=request,
        )

    transport = httpx.MockTransport(mock_handler)
    client = CalendlyClient(api_key="mock_pat", transport=transport)
    invitees = client.list_event_invitees("evt_uuid_123")
    assert len(invitees) == 2
    assert invitees[0]["name"] == "Invitee 1"
    assert invitees[1]["name"] == "Invitee 2"
    client.close()


def test_event_to_document_formatting():
    """Event document formats meeting metadata and invitee Q&A responses."""
    event = {
        "uri": "https://api.calendly.com/scheduled_events/e1111111-2222-3333-4444-555555555555",
        "name": "Design Sync",
        "status": "active",
        "start_time": "2026-10-15T15:00:00Z",
        "end_time": "2026-10-15T15:30:00Z",
        "created_at": "2026-10-01T09:00:00Z",
        "updated_at": "2026-10-01T09:00:00Z",
        "location": {"type": "google_meet", "join_url": "https://meet.google.com/abc-defg-hij"},
        "meeting_notes_plain": "Discuss Q4 frontend roadmap and new connector architecture.",
        "event_memberships": [{"user_name": "Alice Host", "user_email": "alice@company.com"}],
    }
    invitees = [
        {
            "name": "Bob Invitee",
            "email": "bob@client.com",
            "status": "active",
            "timezone": "America/Los_Angeles",
            "questions_and_answers": [
                {
                    "question": "What topics do you want to cover?",
                    "answer": "Knowledge graph pipelines and vector ingestion performance.",
                }
            ],
        }
    ]

    doc = _event_to_document(event, invitees)
    assert doc["id"] == "calendly:e1111111-2222-3333-4444-555555555555"
    assert doc["data_id"].startswith("calendly:")
    assert doc["name"] == "Design Sync"
    assert doc["external_metadata"]["source"] == CALENDLY_SOURCE_NAME
    assert doc["external_metadata"]["invitee_count"] == 1

    text = doc["text"]
    assert "# Scheduled Event: Design Sync" in text
    assert "https://meet.google.com/abc-defg-hij" in text
    assert "Alice Host <alice@company.com>" in text
    assert "Discuss Q4 frontend roadmap" in text
    assert "### Invitee 1: Bob Invitee <bob@client.com>" in text
    assert "Q: What topics do you want to cover?" in text
    assert "A: Knowledge graph pipelines and vector ingestion performance." in text


def test_event_to_document_cancellation():
    """Event document includes cancellation reason when event is canceled."""
    event = {
        "uri": "https://api.calendly.com/scheduled_events/evt_canceled",
        "name": "Cancelled Sync",
        "status": "canceled",
        "start_time": "2026-10-15T15:00:00Z",
        "end_time": "2026-10-15T15:30:00Z",
        "location": None,
    }
    invitees = [
        {
            "name": "Charlie",
            "email": "charlie@test.com",
            "status": "canceled",
            "cancellation": {"canceled_by": "Host", "reason": "Conflict with company all-hands"},
        }
    ]

    doc = _event_to_document(event, invitees)
    assert doc["status"] == "canceled"
    assert "**Cancellation**: Canceled by Host (Conflict with company all-hands)" in doc["text"]


def test_source_metadata_document_mode():
    """calendly_source declares DOCUMENT_SOURCE_ATTR and cognee_document_source."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, request=request)

    client = CalendlyClient(api_key="mock_pat", transport=httpx.MockTransport(mock_handler))
    source = calendly_source(client=client)

    assert source.cognee_document_source == "calendly"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == "calendly"
    assert CALENDLY_TABLE_NAME in source.resources
    assert source.resources[CALENDLY_TABLE_NAME].write_disposition == "replace"
    client.close()


def test_source_basic_ingestion(tmp_path):
    """Source executes through dlt pipeline and yields expected documents."""
    events_payload = {
        "collection": [
            {
                "uri": "https://api.calendly.com/scheduled_events/evt_111",
                "name": "Onboarding Call",
                "status": "active",
                "start_time": "2026-10-10T10:00:00.000000Z",
                "end_time": "2026-10-10T10:30:00.000000Z",
                "created_at": "2026-10-01T00:00:00Z",
                "updated_at": "2026-10-01T00:00:00Z",
            }
        ],
        "pagination": {"next_page_token": None},
    }
    invitees_payload = {
        "collection": [
            {
                "name": "New User",
                "email": "user@newcorp.com",
                "status": "active",
                "questions_and_answers": [
                    {"question": "Role at company?", "answer": "Founding Engineer"}
                ],
            }
        ],
        "pagination": {"next_page_token": None},
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/users/me" in url_str:
            return httpx.Response(
                200,
                json={"resource": {"uri": "https://api.calendly.com/users/U1"}},
                request=request,
            )
        if "/scheduled_events/evt_111/invitees" in url_str:
            return httpx.Response(200, json=invitees_payload, request=request)
        if "/scheduled_events" in url_str:
            return httpx.Response(200, json=events_payload, request=request)
        return httpx.Response(404, request=request)

    client = CalendlyClient(api_key="mock_pat", transport=httpx.MockTransport(mock_handler))
    source = calendly_source(client=client)

    pipeline = dlt.pipeline(
        pipeline_name="test_calendly_pipe",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )
    load_info = pipeline.run(source)
    assert load_info.has_failed_jobs is False

    items = list(source.resources[CALENDLY_TABLE_NAME]())
    assert len(items) == 1
    assert items[0]["name"] == "Onboarding Call"
    assert "Founding Engineer" in items[0]["text"]
    client.close()


def test_source_incremental_cursor_advancement(tmp_path):
    """Source advances watermark cursor in dlt state to the latest seen start_time."""
    events_payload = {
        "collection": [
            {
                "uri": "https://api.calendly.com/scheduled_events/evt_101",
                "name": "Meeting 1",
                "status": "active",
                "start_time": "2026-10-10T12:00:00.000000Z",
            },
            {
                "uri": "https://api.calendly.com/scheduled_events/evt_102",
                "name": "Meeting 2",
                "status": "active",
                "start_time": "2026-10-12T15:00:00.000000Z",
            },
        ],
        "pagination": {"next_page_token": None},
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/scheduled_events" in url_str:
            return httpx.Response(200, json=events_payload, request=request)
        if "/invitees" in url_str:
            return httpx.Response(200, json={"collection": [], "pagination": {}}, request=request)
        return httpx.Response(200, json={"resource": {"uri": "user_uri"}}, request=request)

    client = CalendlyClient(api_key="mock_pat", transport=httpx.MockTransport(mock_handler))
    source = calendly_source(user_uri="user_uri", client=client)

    pipeline = dlt.pipeline(
        pipeline_name="test_cursor_pipe",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )
    pipeline.run(source)

    state = pipeline.state.get("sources", {}).get(CALENDLY_SOURCE_NAME, {})
    resource_state = state.get("resources", {}).get(CALENDLY_TABLE_NAME, {})
    assert resource_state.get("last_min_start_time") == "2026-10-12T15:00:00.000000Z"
    client.close()


def test_client_network_error_retry(monkeypatch):
    """Client retries on network error and succeeds."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    attempts = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.NetworkError("Connection reset by peer")
        return httpx.Response(
            200,
            json={"resource": {"uri": "https://api.calendly.com/users/U1"}},
            request=request,
        )

    transport = httpx.MockTransport(mock_handler)
    client = CalendlyClient(api_key="mock_pat", transport=transport)
    user = client.get_current_user()
    assert attempts == 2
    assert user["uri"] == "https://api.calendly.com/users/U1"
    client.close()


def test_source_auto_resolves_user_uri(tmp_path):
    """calendly_source queries /users/me if user_uri and organization_uri are omitted."""
    called_users_me = False

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called_users_me
        url_str = str(request.url)
        if "/users/me" in url_str:
            called_users_me = True
            return httpx.Response(
                200,
                json={"resource": {"uri": "https://api.calendly.com/users/AUTODISCOVERED"}},
                request=request,
            )
        if "/scheduled_events" in url_str:
            assert "user=https%3A%2F%2Fapi.calendly.com%2Fusers%2FAUTODISCOVERED" in url_str
            return httpx.Response(200, json={"collection": []}, request=request)
        return httpx.Response(404, request=request)

    client = CalendlyClient(api_key="mock_pat", transport=httpx.MockTransport(mock_handler))
    source = calendly_source(client=client)

    pipeline = dlt.pipeline(
        pipeline_name="test_auto_user_pipe",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )
    pipeline.run(source)
    assert called_users_me is True
    client.close()


def test_source_skip_invitees_when_disabled(tmp_path):
    """calendly_source does not query /invitees when include_invitee_qa=False."""
    called_invitees = False

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal called_invitees
        url_str = str(request.url)
        if "/users/me" in url_str:
            return httpx.Response(200, json={"resource": {"uri": "user_uri"}}, request=request)
        if "/invitees" in url_str:
            called_invitees = True
            return httpx.Response(200, json={"collection": []}, request=request)
        if "/scheduled_events" in url_str:
            return httpx.Response(
                200,
                json={
                    "collection": [
                        {
                            "uri": "https://api.calendly.com/scheduled_events/evt_99",
                            "name": "Sync",
                            "status": "active",
                        }
                    ]
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    client = CalendlyClient(api_key="mock_pat", transport=httpx.MockTransport(mock_handler))
    source = calendly_source(user_uri="user_uri", include_invitee_qa=False, client=client)

    pipeline = dlt.pipeline(
        pipeline_name="test_skip_invitees_pipe",
        destination=dlt.destinations.duckdb(credentials=f"{tmp_path}/test.duckdb"),
        pipelines_dir=str(tmp_path),
    )
    pipeline.run(source)
    assert called_invitees is False
    client.close()
