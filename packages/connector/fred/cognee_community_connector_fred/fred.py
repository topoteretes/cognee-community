"""FRED (Federal Reserve Economic Data) connector for cognee.

Syncs US macroeconomic data series (GDP, unemployment, inflation, interest rates, etc.)
from the Federal Reserve Bank of St. Louis (fred.stlouisfed.org) into Cognee memory
as structured, searchable documents for financial and economic reasoning.

Key Features:
- Direct REST integration with FRED API (https://api.stlouisfed.org/fred)
- Ingests series metadata (title, units, frequency, seasonal adjustment, notes)
- Formats recent observations as clean time-series data without float noise
- Incremental sync tracking: skips unchanged series if no new observations exist
- Robust rate limiting backoff (HTTP 429) & connection retries
- DLT resource supporting write_disposition="merge" and DOCUMENT_SOURCE_ATTR="fred"
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

try:
    import dlt
except ImportError:
    dlt = None

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

logger = logging.getLogger("cognee_connector_fred")

FRED_API_BASE = "https://api.stlouisfed.org/fred"
FRED_WEB_BASE = "https://fred.stlouisfed.org"

_MAX_RETRIES = 3
_DEFAULT_OBSERVATION_LIMIT = 60


class FredConnectorError(RuntimeError):
    """Raised when a FRED API request fails unrecoverably."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class FredRenderOptions:
    """Configurable rendering options for FRED documents."""

    max_observations: int = _DEFAULT_OBSERVATION_LIMIT
    include_notes: bool = True
    include_discontinued_warning: bool = True


class FredClient:
    """Client for the Federal Reserve Economic Data (FRED) REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        client: httpx.Client | None = None,
    ):
        if not api_key:
            raise ValueError("FRED API key is required. Obtain a free key at https://fred.stlouisfed.org/docs/api/api_key.html")

        self.api_key = api_key
        self.base_url = (base_url or FRED_API_BASE).rstrip("/")
        self._client = client or httpx.Client()
        self._owns_client = client is None

    def close(self):
        """Close underlying HTTP client if owned."""
        if self._owns_client:
            self._client.close()

    def _get_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-fred/0.1.0 (Cognee AI Memory)",
        }

    def request(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send authenticated request to FRED endpoint with automatic backoff on 429."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        query_params = dict(params or {})
        query_params["api_key"] = self.api_key
        query_params["file_type"] = "json"

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._client.get(
                    url,
                    headers=self._get_headers(),
                    params=query_params,
                    timeout=30.0,
                )

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "2"))
                    logger.warning("FRED API rate limit hit, sleeping for %ds", retry_after)
                    time.sleep(retry_after)
                    continue

                if resp.status_code == 400:
                    err_msg = resp.json().get("error_message", resp.text) if "application/json" in resp.headers.get("content-type", "") else resp.text
                    raise FredConnectorError(f"Bad Request: {err_msg}", 400)

                if resp.status_code in (401, 403):
                    raise FredConnectorError("Unauthorized/Forbidden: Invalid or disabled FRED API key", resp.status_code)

                if resp.status_code == 404:
                    raise FredConnectorError(f"FRED resource not found: {endpoint}", 404)

                if resp.status_code >= 500:
                    raise FredConnectorError(f"FRED server error ({resp.status_code}): {resp.text}", resp.status_code)

                return resp.json()

            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                if attempt == _MAX_RETRIES:
                    raise FredConnectorError(f"FRED connection failed after {attempt} attempts: {exc}") from exc
                time.sleep(1.0 * attempt)

        raise FredConnectorError("Max retries exceeded for FRED request")

    def get_series(self, series_id: str) -> dict[str, Any]:
        """Fetch metadata for a single FRED series."""
        data = self.request("series", params={"series_id": series_id.strip()})
        seriess = data.get("seriess", [])
        if not seriess:
            raise FredConnectorError(f"Series '{series_id}' not found in FRED response", 404)
        return seriess[0]

    def get_series_observations(
        self,
        series_id: str,
        observation_start: str | None = None,
        limit: int = _DEFAULT_OBSERVATION_LIMIT,
        sort_order: str = "desc",
    ) -> list[dict[str, str]]:
        """Fetch observations for a series, ordered by date descending."""
        params: dict[str, Any] = {
            "series_id": series_id.strip(),
            "limit": limit,
            "sort_order": sort_order,
        }
        if observation_start:
            params["observation_start"] = observation_start

        data = self.request("series/observations", params=params)
        return data.get("observations", [])

    def search_series(self, search_text: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search FRED for series matching search query."""
        params = {"search_text": search_text.strip(), "limit": limit}
        data = self.request("series/search", params=params)
        return data.get("seriess", [])


def render_fred_document(
    series: dict[str, Any],
    observations: list[dict[str, str]],
    options: FredRenderOptions = FredRenderOptions(),
) -> dict[str, Any]:
    """Convert FRED series metadata and observations into a clean Cognee document."""
    series_id = series.get("id", "").strip()
    title = series.get("title", f"FRED Series {series_id}")
    units = series.get("units", "Units not specified")
    frequency = series.get("frequency", "Unknown frequency")
    seasonal_adj = series.get("seasonal_adjustment", "Not Seasonally Adjusted")
    last_updated = series.get("last_updated", "Unknown")
    notes = series.get("notes", "").strip() if options.include_notes else ""
    web_url = f"{FRED_WEB_BASE}/series/{series_id}"

    # Build observation block (strings preserved to prevent float formatting artifacts)
    obs_lines = []
    # If observations were fetched descending, show chronologically or descending cleanly
    for obs in observations[:options.max_observations]:
        date = obs.get("date", "")
        val = obs.get("value", "")
        if date and val:
            obs_lines.append(f"- {date}: {val}")

    obs_block = "\n".join(obs_lines) if obs_lines else "No recent observations recorded."

    content_parts = [
        f"# FRED Economic Series: {title} ({series_id})",
        f"- Series ID: {series_id}",
        f"- Frequency: {frequency}",
        f"- Units: {units}",
        f"- Seasonal Adjustment: {seasonal_adj}",
        f"- Last Updated: {last_updated}",
        f"- URL: {web_url}",
    ]

    if notes:
        # Truncate overly long notes if exceeding 1500 chars
        clean_notes = notes[:1500] + ("..." if len(notes) > 1500 else "")
        content_parts.append(f"- Description & Notes: {clean_notes}")

    content_parts.append(f"\n## Recent Observations ({len(obs_lines)} data points):")
    content_parts.append(obs_block)

    full_content = "\n".join(content_parts)

    return {
        "id": f"fred:{series_id}",
        "title": f"FRED: {title} ({series_id})",
        "content": full_content,
        "url": web_url,
        "_deleted": False,
        "series_id": series_id,
        "units": units,
        "frequency": frequency,
        "observation_count": len(obs_lines),
        "latest_observation_date": observations[0].get("date") if observations else None,
    }


def sync_fred_records(
    client: FredClient,
    series_ids: list[str],
    options: FredRenderOptions = FredRenderOptions(),
    last_observation_dates: dict[str, str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Iterate through requested series IDs and yield rendered document rows.

    Supports incremental sync: if `last_observation_dates` has the latest date seen,
    and no newer observations exist, the series is skipped to avoid redundant LLM indexing.
    """
    last_observation_dates = last_observation_dates or {}

    for raw_id in series_ids:
        series_id = raw_id.strip()
        if not series_id:
            continue

        try:
            meta = client.get_series(series_id)
        except Exception as exc:
            logger.error("Failed fetching metadata for FRED series '%s': %s", series_id, exc)
            continue

        last_seen_date = last_observation_dates.get(series_id)

        try:
            obs = client.get_series_observations(
                series_id,
                observation_start=last_seen_date,
                limit=options.max_observations,
                sort_order="desc",
            )
        except Exception as exc:
            logger.error("Failed fetching observations for FRED series '%s': %s", series_id, exc)
            obs = []

        # If incremental mode and no newer observations returned
        if last_seen_date:
            latest_date = obs[0].get("date") if obs else None
            if not obs or (latest_date and latest_date <= last_seen_date):
                logger.info("FRED series '%s' has no new observations since %s; skipping.", series_id, last_seen_date)
                continue

        yield render_fred_document(meta, obs, options)


# ─── DLT Source & Fallback Generator ──────────────────────────────────────────


if dlt is not None:

    @dlt.source(name="fred")
    def fred_source(
        api_key: str,
        series_ids: list[str] | None = None,
        search_query: str | None = None,
        options: FredRenderOptions = FredRenderOptions(),
    ):
        """DLT source to sync Federal Reserve Economic Data into Cognee AI memory.

        Args:
            api_key: Free FRED API key.
            series_ids: List of FRED series symbols (e.g. ['GDP', 'UNRATE', 'FEDFUNDS', 'CPIAUCSL']).
            search_query: Optional search keyword to discover series automatically.
            options: FredRenderOptions for document formatting.
        """
        client = FredClient(api_key=api_key)
        target_ids = list(series_ids or [])

        if search_query:
            try:
                results = client.search_series(search_query, limit=5)
                for item in results:
                    s_id = item.get("id")
                    if s_id and s_id not in target_ids:
                        target_ids.append(s_id)
            except Exception as e:
                logger.warning("Failed executing FRED search query '%s': %s", search_query, e)

        if not target_ids:
            # Default popular macroeconomic benchmarks
            target_ids = ["GDP", "UNRATE"]

        @dlt.resource(name="fred_series", primary_key="id", write_disposition="merge")
        def fred_series():
            # In DLT, state can store latest observation dates for incremental sync
            try:
                state = dlt.current.resource_state().setdefault("latest_dates", {})
            except Exception:
                state = {}

            for doc in sync_fred_records(client, target_ids, options=options, last_observation_dates=state):
                latest_date = doc.get("latest_observation_date")
                if latest_date:
                    state[doc["series_id"]] = latest_date
                yield doc

        setattr(fred_series, DOCUMENT_SOURCE_ATTR, "fred")
        return [fred_series]

else:

    def fred_source(
        api_key: str,
        series_ids: list[str] | None = None,
        search_query: str | None = None,
        options: FredRenderOptions = FredRenderOptions(),
    ):
        """Fallback generator when dlt is not loaded."""
        client = FredClient(api_key=api_key)
        target_ids = list(series_ids or ["GDP", "UNRATE"])
        return list(sync_fred_records(client, target_ids, options=options))
