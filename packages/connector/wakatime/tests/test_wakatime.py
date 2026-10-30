"""Unit tests for the WakaTime data-source connector.

Hermetic tests runnable in CI without live network access or WakaTime credentials.
"""

from typing import Any
import pytest

from cognee_community_connector_wakatime.wakatime import (
    DOCUMENT_SOURCE_ATTR,
    WAKATIME_SOURCE_NAME,
    WAKATIME_TABLE_NAME,
    WakaTimeClient,
    _stats_to_row,
    _summary_to_row,
    wakatime_source,
)


class FakeWakaTimeHttpClient:
    """Mock HTTP client returning synthetic WakaTime API payloads."""

    def __init__(self, routes: dict[str, Any] | None = None):
        self.routes = routes or {}

    def get(self, url: str, params: dict[str, Any] | None = None, **kwargs):
        for pattern, response_data in self.routes.items():
            if pattern in url:
                class MockResponse:
                    status_code = 200

                    def json(self):
                        return response_data

                    def raise_for_status(self):
                        pass

                return MockResponse()

        raise RuntimeError(f"Unmocked URL called: {url}")


SAMPLE_DAILY_SUMMARY = {
    "grand_total": {
        "text": "5 hrs 20 mins",
        "total_seconds": 19200,
    },
    "range": {
        "date": "2026-10-24",
    },
    "projects": [
        {"name": "cognee", "text": "4 hrs 10 mins", "percent": 78.1},
        {"name": "thebhom", "text": "1 hr 10 mins", "percent": 21.9},
    ],
    "languages": [
        {"name": "Python", "text": "4 hrs 10 mins", "percent": 78.1},
        {"name": "TypeScript", "text": "1 hr 10 mins", "percent": 21.9},
    ],
    "editors": [
        {"name": "VS Code", "text": "5 hrs 20 mins"},
    ],
}

SAMPLE_STATS = {
    "human_readable_range": "Last 7 Days",
    "human_readable_total": "32 hrs 45 mins",
    "human_readable_daily_average": "4 hrs 40 mins",
    "projects": [
        {"name": "cognee", "text": "22 hrs 30 mins", "percent": 68.7},
        {"name": "thebhom", "text": "10 hrs 15 mins", "percent": 31.3},
    ],
    "languages": [
        {"name": "Python", "text": "25 hrs 10 mins", "percent": 76.8},
        {"name": "HTML", "text": "7 hrs 35 mins", "percent": 23.2},
    ],
    "editors": [
        {"name": "VS Code", "text": "32 hrs 45 mins", "percent": 100.0},
    ],
}


def test_summary_to_row_formatting():
    """Test converting daily summary dictionary into a clean markdown document."""
    row = _summary_to_row(SAMPLE_DAILY_SUMMARY)
    assert row is not None
    assert row["id"] == "wakatime_2026-10-24_all"
    assert "WakaTime Coding Activity" in row["title"]
    assert "5 hrs 20 mins" in row["title"]
    assert "# WakaTime Coding Activity" in row["content"]
    assert "**cognee**: 4 hrs 10 mins (78.1%)" in row["content"]
    assert "**Python**: 4 hrs 10 mins (78.1%)" in row["content"]
    assert "**VS Code**: 5 hrs 20 mins" in row["content"]


def test_stats_to_row_overview():
    """Test generating a multi-day developer productivity overview document."""
    row = _stats_to_row(SAMPLE_STATS)
    assert row is not None
    assert row["id"] == "wakatime_productivity_overview_7d"
    assert "Last 7 Days" in row["title"]
    assert "**Total Tracked Time:** 32 hrs 45 mins" in row["content"]
    assert "**Python**: 25 hrs 10 mins (76.8%)" in row["content"]


def test_wakatime_source_dlt_configuration():
    """Test that wakatime_source configures replace disposition and document mode."""
    routes = {
        "/users/current/summaries": {"data": [SAMPLE_DAILY_SUMMARY]},
        "/users/current/stats/last_7_days": {"data": SAMPLE_STATS},
    }
    client = WakaTimeClient(
        api_key="mock_test_key",
        http_client=FakeWakaTimeHttpClient(routes),
    )

    source = wakatime_source(
        client=client,
        start_date="2026-10-20",
        end_date="2026-10-24",
    )

    assert getattr(source, DOCUMENT_SOURCE_ATTR) == WAKATIME_SOURCE_NAME
    assert WAKATIME_TABLE_NAME in source.resources

    resource = source.resources[WAKATIME_TABLE_NAME]
    assert resource.write_disposition == "replace"

    items = list(resource)
    assert len(items) == 2
    item_ids = [it["id"] for it in items]
    assert "wakatime_2026-10-24_all" in item_ids
    assert "wakatime_productivity_overview_7d" in item_ids
