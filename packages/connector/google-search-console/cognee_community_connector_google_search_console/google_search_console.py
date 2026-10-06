"""Google Search Console connector for cognee (snapshot / incremental + forget-on-delete).

Fetches Google Search Console search performance data (queries, pages, clicks,
impressions, CTR, and average position) and formats them as structured markdown
documents for cognee's ingestion pipeline.

Unlike tabular-only ingestion, Search Console performance records are ingested as
rich documents: the source declares ``DOCUMENT_SOURCE_ATTR`` as ``"google_search_console"``,
so cognee's ``resolve_dlt_sources`` routes them through normal document parsing and
the cognify entity-extraction pipeline. This allows natural language queries such as
"Which queries drove the most clicks to our documentation?" or "What is our average
ranking position for graph rag?".

Design
------
* **Auth** — OAuth 2.0 or direct Bearer token. Supports pre-generated access tokens,
  OAuth client ID + client secret + refresh token (with automatic refresh),
  installed-app credentials JSON / token JSON files, or injected HTTP clients.
* **Property Discovery & Scope** — Discovers all verified Search Console properties
  (URL-prefix and domain properties) via the Sites API, or scopes ingestion to
  user-selected ``site_urls``.
* **Search Analytics Ingestion** — Queries the Search Analytics API with configurable
  dimensions (default: ``["query", "page"]``), date windows, and row limits.
* **Pagination** — Walks through large Search Analytics result sets using ``startRow``
  and ``rowLimit`` without loading entire datasets into memory.
* **Incremental Sync** — Uses a date-window cursor persisted in dlt resource state.
  Because Google Search Console data lags by ~2-3 days and recent data can change,
  subsequent syncs re-query a configurable trailing window (default 3 days) and
  advance the cursor only when the full window is successfully processed.
* **Forget-on-Delete** — Under the default snapshot mode (``write_disposition="replace"``),
  deselected or upstream-deleted properties fall out of staging, and cognee's
  ``orphan_cleanup`` removes them from graph and vector stores. Under
  ``write_disposition="merge"``, removed properties emit explicit ``_deleted=True`` markers.
* **Resilience** — Automatically retries rate limits (HTTP 429) and transient server errors
  (HTTP 5xx) with exponential backoff and ``Retry-After`` header support.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from cognee.shared.logging_utils import get_logger

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:  # pragma: no cover
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

logger = get_logger("google_search_console_connector")

GSC_TABLE_NAME = "google_search_console_performance"
GSC_SOURCE_NAME = "google_search_console"
GSC_API_BASE_URL = "https://www.googleapis.com/webmasters/v3"
GSC_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GSC_READONLY_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"

_MAX_RETRIES = 5
_DEFAULT_ROW_LIMIT = 1000
_MAX_ROW_LIMIT = 25000
_DEFAULT_LOOKBACK_DAYS = 28
_DEFAULT_TRAILING_DAYS = 3
_DATA_LAG_DAYS = 3


# ---------------------------------------------------------------------------
# API Client & Auth
# ---------------------------------------------------------------------------
class GoogleSearchConsoleClient:
    """HTTP client for the Google Search Console (Webmasters) API v3.

    Handles token resolution, OAuth2 refresh, rate-limit retries, and endpoint calls.
    """

    def __init__(
        self,
        token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        credentials_path: str | None = None,
        token_path: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._token = (
            token
            or os.environ.get("GOOGLE_SEARCH_CONSOLE_ACCESS_TOKEN")
            or os.environ.get("GSC_ACCESS_TOKEN")
        )
        self._client_id = (
            client_id
            or os.environ.get("GOOGLE_SEARCH_CONSOLE_CLIENT_ID")
            or os.environ.get("GSC_CLIENT_ID")
        )
        self._client_secret = (
            client_secret
            or os.environ.get("GOOGLE_SEARCH_CONSOLE_CLIENT_SECRET")
            or os.environ.get("GSC_CLIENT_SECRET")
        )
        self._refresh_token = (
            refresh_token
            or os.environ.get("GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN")
            or os.environ.get("GSC_REFRESH_TOKEN")
        )
        self._credentials_path = credentials_path
        self._token_path = token_path
        self._http_client = http_client or httpx.Client(timeout=30.0)

        self._load_credentials_files()

    def _load_credentials_files(self) -> None:
        """Load OAuth secrets and tokens from files if provided."""
        if self._token_path and os.path.exists(self._token_path):
            try:
                with open(self._token_path, encoding="utf-8") as f:
                    data = json.load(f)
                    self._token = data.get("access_token") or self._token
                    self._refresh_token = data.get("refresh_token") or self._refresh_token
            except Exception as exc:
                logger.warning("Failed to load token file %s: %s", self._token_path, exc)

        if self._credentials_path and os.path.exists(self._credentials_path):
            try:
                with open(self._credentials_path, encoding="utf-8") as f:
                    data = json.load(f)
                    installed = data.get("installed") or data.get("web") or {}
                    self._client_id = installed.get("client_id") or self._client_id
                    self._client_secret = installed.get("client_secret") or self._client_secret
            except Exception as exc:
                logger.warning(
                    "Failed to load credentials file %s: %s", self._credentials_path, exc
                )

    def refresh_access_token(self) -> str:
        """Refresh the OAuth2 access token using the refresh token."""
        if not self._refresh_token or not self._client_id or not self._client_secret:
            raise ValueError(
                "Cannot refresh token without refresh_token, client_id, and client_secret."
            )

        payload = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
        }
        resp = self._http_client.post(GSC_TOKEN_ENDPOINT, data=payload)
        resp.raise_for_status()
        data = resp.json()
        new_token = data.get("access_token")
        if not new_token:
            raise ValueError("Token response did not contain access_token.")
        self._token = new_token
        return new_token

    def get_auth_header(self) -> dict[str, str]:
        """Return the authorization headers, refreshing token if necessary."""
        if not self._token and self._refresh_token:
            self.refresh_access_token()

        if not self._token:
            raise ValueError(
                "Google Search Console authentication requires a valid access token or "
                "OAuth refresh configuration. Pass token=, provide client credentials, "
                "or set GSC_ACCESS_TOKEN."
            )

        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Execute an HTTP request with retry logic for 429 and 5xx responses."""
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self.get_auth_header())

        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._http_client.request(method, url, headers=headers, **kwargs)
                if resp.status_code == 401 and self._refresh_token and attempt < _MAX_RETRIES - 1:
                    logger.info("Search Console access token expired; refreshing...")
                    self.refresh_access_token()
                    headers.update(self.get_auth_header())
                    continue

                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt == _MAX_RETRIES - 1:
                        resp.raise_for_status()
                    delay = _get_retry_delay(resp.headers, attempt)
                    logger.warning(
                        "Search Console API %s returned %d; retrying in %.1fs (%d/%d)...",
                        url,
                        resp.status_code,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue

                resp.raise_for_status()
                return resp
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise
                delay = float(2**attempt)
                logger.warning(
                    "Network error (%s) contacting Search Console; retrying in %.1fs (%d/%d)...",
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)

        raise RuntimeError("Unexpected failure in GoogleSearchConsoleClient._request")

    def list_sites(self) -> list[dict[str, Any]]:
        """List all verified sites/properties the authenticated user has access to."""
        url = f"{GSC_API_BASE_URL}/sites"
        resp = self._request("GET", url)
        data = resp.json()
        return data.get("siteEntry") or []

    def query_search_analytics(
        self,
        site_url: str,
        start_date: str,
        end_date: str,
        dimensions: list[str] | None = None,
        row_limit: int = _DEFAULT_ROW_LIMIT,
        start_row: int = 0,
        data_state: str = "final",
        dimension_filter_groups: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Query Search Analytics for a specific property."""
        encoded_site = quote(site_url, safe="")
        url = f"{GSC_API_BASE_URL}/sites/{encoded_site}/searchAnalytics/query"
        payload: dict[str, Any] = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dimensions or ["query", "page"],
            "rowLimit": min(row_limit, _MAX_ROW_LIMIT),
            "startRow": start_row,
            "dataState": data_state,
        }
        if dimension_filter_groups:
            payload["dimensionFilterGroups"] = dimension_filter_groups

        resp = self._request("POST", url, json=payload)
        return resp.json()


def _get_retry_delay(headers: httpx.Headers | dict[str, str], attempt: int) -> float:
    """Calculate retry delay from Retry-After header or exponential backoff."""
    retry_after = headers.get("retry-after") if hasattr(headers, "get") else None
    if retry_after:
        try:
            return float(retry_after)
        except (ValueError, TypeError):
            pass
    return float(2**attempt)


# ---------------------------------------------------------------------------
# Document Formatting
# ---------------------------------------------------------------------------
def _row_to_document(
    row: dict[str, Any],
    site_url: str,
    dimensions: list[str],
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Convert a raw Search Analytics row into a rich markdown document."""
    keys = row.get("keys", [])
    dim_map = dict(zip(dimensions, keys, strict=False))

    query = dim_map.get("query", "")
    page = dim_map.get("page", "")
    country = dim_map.get("country", "")
    device = dim_map.get("device", "")
    date_val = dim_map.get("date", "")

    clicks = int(row.get("clicks", 0))
    impressions = int(row.get("impressions", 0))
    ctr = float(row.get("ctr", 0.0))
    position = float(row.get("position", 0.0))

    # Construct stable, deterministic primary key ID
    key_components = [site_url]
    for dim in dimensions:
        key_components.append(str(dim_map.get(dim, "")))
    doc_id = f"gsc:{':'.join(key_components)}"

    landing_url = page if page else site_url
    query_title = f'"{query}"' if query else "Overall Performance"
    title = f"Search Performance: {query_title} ({site_url})"

    # Compose structured Markdown document for cognify entity extraction & vector search
    content_lines = [
        f"# Google Search Console Performance: {query_title}",
        "",
        f"- **Property**: {site_url}",
        f"- **Query**: {query if query else 'N/A'}",
        f"- **Landing Page**: {page if page else 'N/A'}",
        f"- **Reporting Period**: {start_date} to {end_date}",
    ]

    if date_val:
        content_lines.append(f"- **Date**: {date_val}")
    if country:
        content_lines.append(f"- **Country**: {country.upper()}")
    if device:
        content_lines.append(f"- **Device**: {device.capitalize()}")

    summary_text = (
        f"During the period {start_date} to {end_date}, search query '{query or 'all queries'}' "
        f"on property '{site_url}' generated {clicks:,} clicks across {impressions:,} "
        f"impressions (CTR: {ctr:.2%}) with an average ranking position of {position:.1f}. "
        f"Traffic directed to: {landing_url}."
    )

    content_lines.extend(
        [
            f"- **Clicks**: {clicks:,}",
            f"- **Impressions**: {impressions:,}",
            f"- **Click-Through Rate (CTR)**: {ctr:.2%}",
            f"- **Average Position**: {position:.1f}",
            "",
            "### Summary",
            summary_text,
        ]
    )

    content = "\n".join(content_lines)

    return {
        "id": doc_id,
        "url": landing_url,
        "title": title,
        "content": content,
        "site_url": site_url,
        "query": query,
        "page": page,
        "clicks": clicks,
        "impressions": impressions,
        "ctr": ctr,
        "position": position,
        "start_date": start_date,
        "end_date": end_date,
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# DLT Source Implementation
# ---------------------------------------------------------------------------
def google_search_console_source(
    token: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    refresh_token: str | None = None,
    credentials_path: str | None = None,
    token_path: str | None = None,
    site_urls: list[str] | None = None,
    dimensions: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    trailing_days: int = _DEFAULT_TRAILING_DAYS,
    data_state: str = "final",
    row_limit: int = _DEFAULT_ROW_LIMIT,
    max_rows_per_site: int | None = None,
    write_disposition: str = "replace",
    client: GoogleSearchConsoleClient | None = None,
) -> Any:
    """Create a dlt source that yields Google Search Console performance data as markdown documents.

    Args:
        token: Access token (or Bearer token) for Google Search Console API.
        client_id: OAuth2 Client ID for automatic token refresh.
        client_secret: OAuth2 Client Secret for automatic token refresh.
        refresh_token: OAuth2 Refresh Token for automatic token refresh.
        credentials_path: Path to OAuth2 credentials JSON file.
        token_path: Path to cached OAuth2 token JSON file.
        site_urls: Explicit list of property URLs (e.g.
            ``["https://example.com/", "sc-domain:example.com"]``). When omitted, all
            verified properties are discovered via the Sites API.
        dimensions: Dimensions to group Search Analytics by (e.g. ``["query", "page"]``).
            Defaults to ``["query", "page"]``.
        start_date: Start date in YYYY-MM-DD format. Defaults to 28 days prior to end date.
        end_date: End date in YYYY-MM-DD format. Defaults to 3 days ago (lag adjustment).
        trailing_days: Number of trailing days to re-sync during incremental runs (default: 3).
        data_state: "final" for finalized data, or "all" to include fresh/unfinalized data.
        row_limit: Batch size per request (max 25,000, default 1,000).
        max_rows_per_site: Optional maximum total rows to ingest per site.
        write_disposition: dlt write disposition (``"replace"`` for full snapshot, or ``"merge"``).
        client: Pre-built ``GoogleSearchConsoleClient`` (useful for test injection).

    Returns:
        A dlt source suitable for ``cognee.add(...)`` or ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            "The Google Search Console connector requires 'dlt': pip install 'dlt[sqlalchemy]'"
        ) from exc

    if client is None:
        client = GoogleSearchConsoleClient(
            token=token,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            credentials_path=credentials_path,
            token_path=token_path,
        )

    selected_dimensions = dimensions or ["query", "page"]

    @dlt.resource(
        name=GSC_TABLE_NAME,
        primary_key="id",
        write_disposition=write_disposition,
    )
    def search_console_performance() -> Any:
        state = dlt.current.resource_state()
        last_synced_date = state.get("last_synced_date")

        # Compute default date window with ~3-day data lag
        today = date.today()
        default_end = (today - timedelta(days=_DATA_LAG_DAYS)).strftime("%Y-%m-%d")
        effective_end = end_date or default_end

        if start_date:
            effective_start = start_date
        elif last_synced_date:
            # Incremental sync: rewind by trailing_days to capture data revisions
            try:
                last_dt = datetime.strptime(last_synced_date, "%Y-%m-%d").date()
                effective_start = (last_dt - timedelta(days=trailing_days)).strftime("%Y-%m-%d")
            except ValueError:
                effective_start = (
                    datetime.strptime(effective_end, "%Y-%m-%d").date()
                    - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
                ).strftime("%Y-%m-%d")
        else:
            # Initial backfill: default 28-day lookback window
            end_dt = datetime.strptime(effective_end, "%Y-%m-%d").date()
            effective_start = (end_dt - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

        logger.info(
            "Google Search Console: syncing window %s to %s (dimensions: %s)",
            effective_start,
            effective_end,
            selected_dimensions,
        )

        # Discover or filter target sites
        active_sites: list[str] = []
        if site_urls is not None:
            active_sites = list(site_urls)
        else:
            try:
                discovered = client.list_sites()
                active_sites = [s.get("siteUrl", "") for s in discovered if s.get("siteUrl")]
                logger.info(
                    "Discovered %d verified Search Console property/properties.", len(active_sites)
                )
            except Exception as exc:
                logger.error("Failed to list Search Console properties: %s", exc)
                raise

        # Previously synced sites that have been removed/deselected (forget-on-delete under merge)
        previously_synced = state.get("synced_site_urls", [])
        if write_disposition == "merge" and previously_synced:
            current_set = set(active_sites)
            for old_site in previously_synced:
                if old_site not in current_set:
                    logger.info("Property %s removed upstream; emitting tombstone.", old_site)
                    yield {"id": f"gsc:{old_site}", "_deleted": True}

        total_synced_rows = 0

        for site_url in active_sites:
            logger.info("Ingesting Search Console performance for site: %s", site_url)
            start_row = 0
            site_row_count = 0

            while True:
                try:
                    response = client.query_search_analytics(
                        site_url=site_url,
                        start_date=effective_start,
                        end_date=effective_end,
                        dimensions=selected_dimensions,
                        row_limit=row_limit,
                        start_row=start_row,
                        data_state=data_state,
                    )
                except httpx.HTTPStatusError as exc:
                    # 403/404 indicates site is unverified or permanently inaccessible; skip
                    if exc.response.status_code in (403, 404):
                        logger.warning(
                            "Property %s returned %d (unverified/inaccessible); skipping: %s",
                            site_url,
                            exc.response.status_code,
                            exc,
                        )
                        break
                    raise

                rows = response.get("rows") or []
                if not rows:
                    break

                for r in rows:
                    yield _row_to_document(
                        row=r,
                        site_url=site_url,
                        dimensions=selected_dimensions,
                        start_date=effective_start,
                        end_date=effective_end,
                    )
                    site_row_count += 1
                    total_synced_rows += 1

                    if max_rows_per_site and site_row_count >= max_rows_per_site:
                        break

                if max_rows_per_site and site_row_count >= max_rows_per_site:
                    break

                # If fewer rows returned than row_limit, pagination has reached the end
                if len(rows) < row_limit:
                    break

                start_row += row_limit

        # Advance cursor only after all sites and batches have been successfully processed
        state["last_synced_date"] = effective_end
        state["synced_site_urls"] = active_sites
        logger.info(
            "Google Search Console: completed sync (%d rows across %d properties). "
            "Cursor advanced to %s.",
            total_synced_rows,
            len(active_sites),
            effective_end,
        )

    @dlt.source(name=GSC_SOURCE_NAME)
    def _gsc_source() -> Any:
        return search_console_performance

    source = _gsc_source()
    setattr(source, DOCUMENT_SOURCE_ATTR, GSC_SOURCE_NAME)
    return source
