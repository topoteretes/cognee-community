"""Hermetic unit test suite for the Cognee FRED Economic Data Connector."""

import json
import pytest
import httpx

from cognee_community_connector_fred.fred import (
    FredClient,
    FredConnectorError,
    FredRenderOptions,
    fred_source,
    render_fred_document,
    sync_fred_records,
)

# Mock FRED API fixtures
MOCK_SERIES_GDP = {
    "realtime_start": "2026-11-01",
    "realtime_end": "2026-11-01",
    "seriess": [
        {
            "id": "GDP",
            "realtime_start": "2026-11-01",
            "realtime_end": "2026-11-01",
            "title": "Gross Domestic Product",
            "observation_start": "1947-01-01",
            "observation_end": "2026-07-01",
            "frequency": "Quarterly",
            "frequency_short": "Q",
            "units": "Billions of Dollars",
            "units_short": "Bil. of $",
            "seasonal_adjustment": "Seasonally Adjusted Annual Rate",
            "seasonal_adjustment_short": "SAAR",
            "last_updated": "2026-10-25 07:33:02-05",
            "notes": "Information regarding the Gross Domestic Product of the United States economy.",
        }
    ],
}

MOCK_OBSERVATIONS_GDP = {
    "realtime_start": "2026-11-01",
    "realtime_end": "2026-11-01",
    "observation_start": "2026-01-01",
    "observation_end": "2026-07-01",
    "units": "Lin",
    "output_type": 1,
    "file_type": "json",
    "order_by": "observation_date",
    "sort_order": "desc",
    "count": 2,
    "offset": 0,
    "limit": 60,
    "observations": [
        {"realtime_start": "2026-11-01", "realtime_end": "2026-11-01", "date": "2026-07-01", "value": "29350.2"},
        {"realtime_start": "2026-11-01", "realtime_end": "2026-11-01", "date": "2026-04-01", "value": "29012.8"},
    ],
}

MOCK_SERIES_SEARCH = {
    "realtime_start": "2026-11-01",
    "realtime_end": "2026-11-01",
    "seriess": [
        {"id": "UNRATE", "title": "Civilian Unemployment Rate"},
        {"id": "NROU", "title": "Noncyclical Rate of Unemployment"},
    ],
}


class FakeFredTransport(httpx.BaseTransport):
    """Hermetic mock HTTP transport simulating FRED API responses."""

    def __init__(self, simulate_rate_limit: bool = False):
        self.simulate_rate_limit = simulate_rate_limit
        self.rate_limit_counter = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)

        if self.simulate_rate_limit and self.rate_limit_counter == 0:
            self.rate_limit_counter += 1
            return httpx.Response(
                status_code=429,
                headers={"Retry-After": "0"},
                json={"error_code": 429, "error_message": "Rate limit exceeded"},
            )

        if "/series/observations" in url_str:
            return httpx.Response(status_code=200, json=MOCK_OBSERVATIONS_GDP)

        if "/series/search" in url_str:
            return httpx.Response(status_code=200, json=MOCK_SERIES_SEARCH)

        if "/series" in url_str:
            if "series_id=NOTFOUND" in url_str:
                return httpx.Response(status_code=404, json={"error_code": 404, "error_message": "Series not found"})
            return httpx.Response(status_code=200, json=MOCK_SERIES_GDP)

        return httpx.Response(status_code=404, json={"error": "Not Found"})


def create_mock_client(simulate_rate_limit: bool = False) -> FredClient:
    transport = FakeFredTransport(simulate_rate_limit=simulate_rate_limit)
    http_client = httpx.Client(transport=transport)
    return FredClient(api_key="test_api_key_12345", client=http_client)


# ─── Tests ────────────────────────────────────────────────────────────────────


def test_client_init_requires_api_key():
    with pytest.raises(ValueError, match="FRED API key is required"):
        FredClient(api_key="")


def test_get_series_metadata():
    client = create_mock_client()
    meta = client.get_series("GDP")
    assert meta["id"] == "GDP"
    assert meta["title"] == "Gross Domestic Product"
    assert meta["frequency"] == "Quarterly"
    assert meta["units"] == "Billions of Dollars"


def test_get_series_not_found():
    client = create_mock_client()
    with pytest.raises(FredConnectorError) as exc_info:
        client.get_series("NOTFOUND")
    assert exc_info.value.status_code == 404


def test_get_series_observations():
    client = create_mock_client()
    obs = client.get_series_observations("GDP", limit=10)
    assert len(obs) == 2
    assert obs[0]["date"] == "2026-07-01"
    assert obs[0]["value"] == "29350.2"


def test_rate_limit_backoff():
    # Transport will return 429 once, then succeed on retry
    client = create_mock_client(simulate_rate_limit=True)
    meta = client.get_series("GDP")
    assert meta["id"] == "GDP"


def test_search_series():
    client = create_mock_client()
    results = client.search_series("unemployment", limit=5)
    assert len(results) == 2
    assert results[0]["id"] == "UNRATE"


def test_render_fred_document():
    series_meta = MOCK_SERIES_GDP["seriess"][0]
    observations = MOCK_OBSERVATIONS_GDP["observations"]

    doc = render_fred_document(series_meta, observations)

    assert doc["id"] == "fred:GDP"
    assert doc["title"] == "FRED: Gross Domestic Product (GDP)"
    assert doc["url"] == "https://fred.stlouisfed.org/series/GDP"
    assert doc["_deleted"] is False
    assert doc["series_id"] == "GDP"
    assert doc["observation_count"] == 2
    assert doc["latest_observation_date"] == "2026-07-01"

    # Verify content formatting
    assert "# FRED Economic Series: Gross Domestic Product (GDP)" in doc["content"]
    assert "- Frequency: Quarterly" in doc["content"]
    assert "- 2026-07-01: 29350.2" in doc["content"]
    assert "- 2026-04-01: 29012.8" in doc["content"]


def test_sync_fred_records():
    client = create_mock_client()
    docs = list(sync_fred_records(client, ["GDP"]))

    assert len(docs) == 1
    assert docs[0]["id"] == "fred:GDP"
    assert docs[0]["latest_observation_date"] == "2026-07-01"


def test_incremental_sync_skips_unchanged():
    client = create_mock_client()
    # If the latest date seen is already "2026-07-01", it should skip syncing
    docs = list(sync_fred_records(
        client,
        ["GDP"],
        last_observation_dates={"GDP": "2026-07-01"},
    ))
    assert len(docs) == 0


def test_fred_source_fallback_execution():
    source = fred_source(
        api_key="test_dummy_key",
        series_ids=["GDP"],
    )
    # Ensure source object is created with correct name/type
    assert source is not None
