"""Unit tests for the Plausible Analytics connector.

All tests run completely offline without live API keys or network access,
using mocked HTTP responses and synthetic analytics fixtures.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_plausible.plausible import (
    PLAUSIBLE_SOURCE_NAME,
    PLAUSIBLE_TABLE_NAME,
    PlausibleAPIError,
    PlausibleAuthError,
    PlausibleClient,
    _build_goals_markdown,
    _build_locations_markdown,
    _build_overview_markdown,
    _build_pages_markdown,
    _build_sources_markdown,
    _redact_token,
    plausible_source,
)

# ---------------------------------------------------------------------------
# Fixtures & Fakes
# ---------------------------------------------------------------------------

SAMPLE_AGGREGATE_RESPONSE = {
    "results": {
        "visitors": {"value": 1420},
        "pageviews": {"value": 5890},
        "bounce_rate": {"value": 42},
        "visit_duration": {"value": 135},
        "events": {"value": 310},
    }
}

SAMPLE_PAGES_RESPONSE = {
    "results": [
        {"page": "/docs/overview", "visitors": 820},
        {"page": "/blog/launch", "visitors": 450},
        {"page": "/pricing", "visitors": 210},
    ]
}

SAMPLE_SOURCES_RESPONSE = {
    "results": [
        {"source": "Google", "visitors": 650},
        {"source": "GitHub", "visitors": 410},
        {"source": "Direct / None", "visitors": 360},
    ]
}

SAMPLE_COUNTRIES_RESPONSE = {
    "results": [
        {"country": "United States", "visitors": 720},
        {"country": "Germany", "visitors": 240},
        {"country": "India", "visitors": 190},
    ]
}

SAMPLE_DEVICES_RESPONSE = {
    "results": [
        {"device": "Desktop", "visitors": 980},
        {"device": "Mobile", "visitors": 440},
    ]
}

SAMPLE_GOALS_RESPONSE = {
    "results": [
        {"goal": "Signup", "events": 145, "visitors": 140},
        {"goal": "Download CLI", "events": 89, "visitors": 82},
    ]
}


class MockTransport(httpx.BaseTransport):
    """Mock HTTP transport returning controlled responses based on URL path/params."""

    def __init__(self, responses: dict[str, Any] | None = None, status_code: int = 200) -> None:
        self.responses = responses or {}
        self.status_code = status_code
        self.call_count = 0
        self.last_request = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        self.last_request = request

        if self.status_code != 200:
            return httpx.Response(status_code=self.status_code, text="Error response")

        url_path = request.url.path
        params = dict(request.url.params)

        if "/aggregate" in url_path:
            return httpx.Response(
                200, json=self.responses.get("aggregate", SAMPLE_AGGREGATE_RESPONSE)
            )

        if "/breakdown" in url_path:
            prop = params.get("property")
            if prop == "event:page":
                return httpx.Response(200, json=self.responses.get("pages", SAMPLE_PAGES_RESPONSE))
            if prop == "visit:source":
                return httpx.Response(
                    200, json=self.responses.get("sources", SAMPLE_SOURCES_RESPONSE)
                )
            if prop == "visit:country":
                return httpx.Response(
                    200, json=self.responses.get("countries", SAMPLE_COUNTRIES_RESPONSE)
                )
            if prop == "visit:device":
                return httpx.Response(
                    200, json=self.responses.get("devices", SAMPLE_DEVICES_RESPONSE)
                )
            if prop == "event:goal":
                return httpx.Response(200, json=self.responses.get("goals", SAMPLE_GOALS_RESPONSE))

        return httpx.Response(200, json={"results": []})


# ---------------------------------------------------------------------------
# Token Redaction & Sanitization Tests
# ---------------------------------------------------------------------------


def test_redact_token_masks_secret() -> None:
    token = "plausible_secret_token_12345"
    message = f"Error communicating with token {token} on endpoint"
    redacted = _redact_token(message, token)

    assert token not in redacted
    assert "[REDACTED_TOKEN]" in redacted


def test_redact_token_noop_on_none_or_short() -> None:
    msg = "Normal message"
    assert _redact_token(msg, None) == msg
    assert _redact_token(msg, "abc") == msg


# ---------------------------------------------------------------------------
# PlausibleClient Tests
# ---------------------------------------------------------------------------


def test_client_get_aggregate() -> None:
    transport = MockTransport()
    client = httpx.Client(transport=transport)
    plausible_client = PlausibleClient(api_key="test_key", client=client)

    result = plausible_client.get_aggregate(site_id="mysite.com", period="30d")

    assert result["results"]["visitors"]["value"] == 1420
    assert result["results"]["pageviews"]["value"] == 5890
    assert transport.call_count == 1
    assert "site_id=mysite.com" in str(transport.last_request.url)


def test_client_get_breakdown() -> None:
    transport = MockTransport()
    client = httpx.Client(transport=transport)
    plausible_client = PlausibleClient(api_key="test_key", client=client)

    pages = plausible_client.get_breakdown("mysite.com", property_name="event:page")

    assert len(pages) == 3
    assert pages[0]["page"] == "/docs/overview"
    assert pages[0]["visitors"] == 820


def test_client_auth_error_401() -> None:
    transport = MockTransport(status_code=401)
    client = httpx.Client(transport=transport)
    plausible_client = PlausibleClient(api_key="my_secret_key_999", client=client)

    with pytest.raises(PlausibleAuthError) as exc_info:
        plausible_client.get_aggregate("mysite.com")

    assert "authentication failed (401)" in str(exc_info.value)
    assert "my_secret_key_999" not in str(exc_info.value)


def test_client_api_error_500() -> None:
    transport = MockTransport(status_code=500)
    client = httpx.Client(transport=transport)
    plausible_client = PlausibleClient(api_key="test_key", client=client)

    with pytest.raises(PlausibleAPIError) as exc_info:
        plausible_client.get_aggregate("mysite.com")

    assert "Plausible API error (500)" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Markdown Formatting Tests
# ---------------------------------------------------------------------------


def test_build_overview_markdown() -> None:
    md = _build_overview_markdown("testsite.org", "30d", SAMPLE_AGGREGATE_RESPONSE)

    assert "# Plausible Analytics Overview: testsite.org" in md
    assert "**Reporting Period**: `30d`" in md
    assert "**Unique Visitors**: 1,420" in md
    assert "**Total Pageviews**: 5,890" in md
    assert "**Bounce Rate**: 42%" in md
    assert "**Average Visit Duration**: 2m 15s" in md


def test_build_pages_markdown_populated() -> None:
    md = _build_pages_markdown("testsite.org", "7d", SAMPLE_PAGES_RESPONSE["results"])

    assert "# Plausible Top Pages & Content: testsite.org" in md
    assert "| 1 | `/docs/overview` | 820 |" in md
    assert "| 2 | `/blog/launch` | 450 |" in md


def test_build_pages_markdown_empty() -> None:
    md = _build_pages_markdown("testsite.org", "7d", [])
    assert "No pageview data recorded for this period." in md


def test_build_sources_markdown() -> None:
    md = _build_sources_markdown("testsite.org", "30d", SAMPLE_SOURCES_RESPONSE["results"])

    assert "# Plausible Traffic Sources & Acquisition: testsite.org" in md
    assert "| 1 | Google | 650 |" in md
    assert "| 2 | GitHub | 410 |" in md


def test_build_goals_markdown() -> None:
    md = _build_goals_markdown("testsite.org", "30d", SAMPLE_GOALS_RESPONSE["results"])

    assert "# Plausible Goals & Conversions: testsite.org" in md
    assert "| **Signup** | 145 | 140 |" in md
    assert "| **Download CLI** | 89 | 82 |" in md


def test_build_locations_markdown() -> None:
    md = _build_locations_markdown(
        "testsite.org",
        "30d",
        SAMPLE_COUNTRIES_RESPONSE["results"],
        SAMPLE_DEVICES_RESPONSE["results"],
    )

    assert "# Plausible Audience Demographics: testsite.org" in md
    assert "| United States | 720 |" in md
    assert "| Desktop | 980 |" in md


# ---------------------------------------------------------------------------
# DLT Source Invariant & Integration Tests
# ---------------------------------------------------------------------------


def test_plausible_source_requires_site_id() -> None:
    with pytest.raises(ValueError, match="site_id must be provided"):
        plausible_source(site_id=None, api_key="some_key")


def test_plausible_source_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLAUSIBLE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="api_key must be provided"):
        plausible_source(site_id="mysite.com", api_key=None)


def test_plausible_source_document_mode_invariant() -> None:
    transport = MockTransport()
    fake_client = PlausibleClient("test_token", client=httpx.Client(transport=transport))

    source = plausible_source(site_id="example.com", client=fake_client)

    # Invariant: Must declare DOCUMENT_SOURCE_ATTR = "plausible" for Cognee entity extraction
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == PLAUSIBLE_SOURCE_NAME


def test_plausible_source_yields_expected_reports() -> None:
    transport = MockTransport()
    fake_client = PlausibleClient("test_token", client=httpx.Client(transport=transport))

    source = plausible_source(
        site_id="example.com",
        period="30d",
        include_breakdowns=True,
        include_goals=True,
        client=fake_client,
    )

    resources = list(source.selected_resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == PLAUSIBLE_TABLE_NAME
    assert resource.write_disposition == "replace"

    # Evaluate the generator and inspect rows
    rows = list(resource())
    assert len(rows) == 5  # overview, pages, sources, demographics, goals

    report_types = [r["metadata"]["report_type"] for r in rows]
    assert report_types == ["overview", "pages", "sources", "demographics", "goals"]

    for r in rows:
        assert r["metadata"]["source"] == PLAUSIBLE_SOURCE_NAME
        assert r["metadata"]["site_id"] == "example.com"
        assert r["metadata"]["period"] == "30d"
        assert len(r["id"]) > 0
        assert r["text"].startswith("# Plausible")


def test_plausible_source_deterministic_id_stability() -> None:
    transport = MockTransport()
    fake_client = PlausibleClient("test_token", client=httpx.Client(transport=transport))

    source_1 = plausible_source(site_id="mysite.com", period="30d", client=fake_client)
    rows_1 = list(next(iter(source_1.selected_resources.values()))())

    source_2 = plausible_source(site_id="mysite.com", period="30d", client=fake_client)
    rows_2 = list(next(iter(source_2.selected_resources.values()))())

    # Deterministic content IDs ensure duplicate runs don't re-cognify identical reports
    assert [r["id"] for r in rows_1] == [r["id"] for r in rows_2]


def test_plausible_source_excludes_breakdowns_when_flag_false() -> None:
    transport = MockTransport()
    fake_client = PlausibleClient("test_token", client=httpx.Client(transport=transport))

    source = plausible_source(
        site_id="example.com",
        period="30d",
        include_breakdowns=False,
        include_goals=False,
        client=fake_client,
    )

    resource = next(iter(source.selected_resources.values()))
    rows = list(resource())

    # Only overview report yielded when breakdowns and goals are disabled
    assert len(rows) == 1
    assert rows[0]["metadata"]["report_type"] == "overview"
