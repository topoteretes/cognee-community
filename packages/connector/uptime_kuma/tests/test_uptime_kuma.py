"""Unit tests for the Uptime Kuma connector.

All tests run completely offline without live server access or network requests,
using synthetic monitoring fixtures and mocked HTTP transports.
"""

from __future__ import annotations

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_uptime_kuma.uptime_kuma import (
    UPTIME_KUMA_SOURCE_NAME,
    UPTIME_KUMA_TABLE_NAME,
    UptimeKumaAPIError,
    UptimeKumaAuthError,
    UptimeKumaClient,
    _build_incidents_markdown,
    _build_monitors_markdown,
    _build_status_page_markdown,
    _redact_token,
    _status_str,
    uptime_kuma_source,
)

# ---------------------------------------------------------------------------
# Synthetic Test Fixtures
# ---------------------------------------------------------------------------

SAMPLE_STATUS_PAGE = {
    "title": "Acme Corp Production Status",
    "description": "Real-time service health board for Acme APIs.",
    "incident": {
        "title": "Elevated Error Rates on Auth API",
        "content": "Engineers are investigating intermittent 502 responses.",
        "style": "warning",
        "createdDate": "2026-10-23 18:30:00",
    },
    "publicGroupList": [
        {
            "id": 1,
            "name": "Core Services",
            "monitorList": [
                {
                    "id": 101,
                    "name": "Auth API",
                    "type": "http",
                    "url": "https://auth.acme.com/health",
                    "status": 0,
                },
                {
                    "id": 102,
                    "name": "Payment Gateway",
                    "type": "http",
                    "url": "https://pay.acme.com/ping",
                    "status": 1,
                },
            ],
        }
    ],
}

SAMPLE_HEARTBEATS = {
    "heartbeatList": {
        "101": [
            {"status": 1, "time": "2026-10-23 18:00:00", "ping": 42},
            {
                "status": 0,
                "time": "2026-10-23 18:25:00",
                "msg": "502 Bad Gateway from upstream",
                "ping": 0,
                "duration": 480,
            },
        ],
        "102": [
            {"status": 1, "time": "2026-10-23 18:25:00", "ping": 18},
        ],
    }
}


class MockTransport(httpx.BaseTransport):
    """Mock HTTP transport returning synthetic Uptime Kuma responses."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.call_count = 0
        self.last_request = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        self.last_request = request

        if self.status_code != 200:
            return httpx.Response(status_code=self.status_code, text="Simulated server error")

        path = request.url.path
        if "/api/status-page/heartbeat/" in path:
            return httpx.Response(200, json=SAMPLE_HEARTBEATS)
        if "/api/status-page/" in path:
            return httpx.Response(200, json=SAMPLE_STATUS_PAGE)
        if "/api/monitor" in path:
            return httpx.Response(
                200, json={"monitors": SAMPLE_STATUS_PAGE["publicGroupList"][0]["monitorList"]}
            )

        return httpx.Response(200, json={})


# ---------------------------------------------------------------------------
# Helper & Token Redaction Tests
# ---------------------------------------------------------------------------


def test_redact_token() -> None:
    token = "secret_kuma_token_xyz"
    msg = f"Failed call with {token} on server"
    redacted = _redact_token(msg, token)

    assert token not in redacted
    assert "[REDACTED_TOKEN]" in redacted


def test_status_str_formatting() -> None:
    assert "Healthy" in _status_str(1)
    assert "Outage" in _status_str(0)
    assert "PENDING" in _status_str(2)
    assert "MAINTENANCE" in _status_str(3)


# ---------------------------------------------------------------------------
# Client Tests
# ---------------------------------------------------------------------------


def test_client_get_status_page() -> None:
    transport = MockTransport()
    client = httpx.Client(transport=transport)
    kuma_client = UptimeKumaClient("http://localhost:3001", client=client)

    data = kuma_client.get_status_page("default")
    assert data["title"] == "Acme Corp Production Status"
    assert transport.call_count == 1
    assert "/api/status-page/default" in str(transport.last_request.url)


def test_client_get_heartbeats() -> None:
    transport = MockTransport()
    client = httpx.Client(transport=transport)
    kuma_client = UptimeKumaClient("http://localhost:3001", client=client)

    data = kuma_client.get_heartbeats("default")
    assert "101" in data["heartbeatList"]
    assert len(data["heartbeatList"]["101"]) == 2


def test_client_auth_error_401() -> None:
    transport = MockTransport(status_code=401)
    client = httpx.Client(transport=transport)
    kuma_client = UptimeKumaClient("http://localhost:3001", api_key="my_kuma_secret", client=client)

    with pytest.raises(UptimeKumaAuthError) as exc_info:
        kuma_client.get_status_page("default")

    assert "authentication failed (401)" in str(exc_info.value)
    assert "my_kuma_secret" not in str(exc_info.value)


def test_client_api_error_500() -> None:
    transport = MockTransport(status_code=500)
    client = httpx.Client(transport=transport)
    kuma_client = UptimeKumaClient("http://localhost:3001", client=client)

    with pytest.raises(UptimeKumaAPIError) as exc_info:
        kuma_client.get_status_page("default")

    assert "Uptime Kuma API error (500)" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Markdown Formatting Tests
# ---------------------------------------------------------------------------


def test_build_monitors_markdown() -> None:
    monitors = SAMPLE_STATUS_PAGE["publicGroupList"][0]["monitorList"]
    md = _build_monitors_markdown("http://localhost:3001", "main", monitors, SAMPLE_HEARTBEATS)

    assert "# Uptime Kuma Monitors: main" in md
    assert "**Auth API**" in md
    assert "https://auth.acme.com/health" in md
    assert "**Payment Gateway**" in md
    assert "18ms" in md


def test_build_incidents_markdown_with_downtime() -> None:
    monitors = SAMPLE_STATUS_PAGE["publicGroupList"][0]["monitorList"]
    md = _build_incidents_markdown("main", monitors, SAMPLE_HEARTBEATS)

    assert "# Uptime Kuma Outages & Incident History: main" in md
    assert "**Auth API**" in md
    assert "502 Bad Gateway from upstream" in md
    assert "480s" in md


def test_build_incidents_markdown_all_healthy() -> None:
    monitors = SAMPLE_STATUS_PAGE["publicGroupList"][0]["monitorList"]
    healthy_heartbeats = {
        "heartbeatList": {
            "102": [{"status": 1, "time": "2026-10-23 18:25:00", "ping": 18}],
        }
    }
    md = _build_incidents_markdown("main", monitors, healthy_heartbeats)

    assert "All monitored services operational" in md


def test_build_status_page_markdown() -> None:
    md = _build_status_page_markdown("main", SAMPLE_STATUS_PAGE)

    assert "# Status Board: Acme Corp Production Status" in md
    assert "Elevated Error Rates on Auth API" in md
    assert "Engineers are investigating intermittent 502 responses" in md


# ---------------------------------------------------------------------------
# DLT Source Invariant & Integration Tests
# ---------------------------------------------------------------------------


def test_uptime_kuma_source_document_mode_invariant() -> None:
    transport = MockTransport()
    fake_client = UptimeKumaClient(
        "http://localhost:3001", client=httpx.Client(transport=transport)
    )

    source = uptime_kuma_source(slug="main", client=fake_client)

    # Invariant: Must declare DOCUMENT_SOURCE_ATTR = "uptime_kuma"
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == UPTIME_KUMA_SOURCE_NAME


def test_uptime_kuma_source_yields_expected_reports() -> None:
    transport = MockTransport()
    fake_client = UptimeKumaClient(
        "http://localhost:3001", client=httpx.Client(transport=transport)
    )

    source = uptime_kuma_source(
        slug="main",
        include_heartbeats=True,
        include_incidents=True,
        client=fake_client,
    )

    resources = list(source.selected_resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == UPTIME_KUMA_TABLE_NAME
    assert resource.write_disposition == "replace"

    rows = list(resource())
    assert len(rows) == 3  # monitors, status_board, incidents

    report_types = [r["metadata"]["report_type"] for r in rows]
    assert report_types == ["monitors", "status_board", "incidents"]

    for r in rows:
        assert r["metadata"]["source"] == UPTIME_KUMA_SOURCE_NAME
        assert r["metadata"]["slug"] == "main"
        assert len(r["id"]) > 0
        assert r["text"].startswith("# ")


def test_uptime_kuma_source_deterministic_id_stability() -> None:
    transport = MockTransport()
    fake_client = UptimeKumaClient(
        "http://localhost:3001", client=httpx.Client(transport=transport)
    )

    source_1 = uptime_kuma_source(slug="main", client=fake_client)
    rows_1 = list(next(iter(source_1.selected_resources.values()))())

    source_2 = uptime_kuma_source(slug="main", client=fake_client)
    rows_2 = list(next(iter(source_2.selected_resources.values()))())

    # Deterministic IDs ensure duplicate runs don't re-cognify identical reports
    assert [r["id"] for r in rows_1] == [r["id"] for r in rows_2]


def test_uptime_kuma_source_excludes_incidents_when_flag_false() -> None:
    transport = MockTransport()
    fake_client = UptimeKumaClient(
        "http://localhost:3001", client=httpx.Client(transport=transport)
    )

    source = uptime_kuma_source(
        slug="main",
        include_heartbeats=False,
        include_incidents=False,
        client=fake_client,
    )

    resource = next(iter(source.selected_resources.values()))
    rows = list(resource())

    # Only monitors and status_board yielded when heartbeats & incidents are disabled
    assert len(rows) == 2
    report_types = [r["metadata"]["report_type"] for r in rows]
    assert report_types == ["monitors", "status_board"]
