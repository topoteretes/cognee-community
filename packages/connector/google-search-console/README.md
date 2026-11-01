# Google Search Console Connector for Cognee

A `dlt` data-source connector that pulls Google Search Console search performance data (queries, landing pages, clicks, impressions, CTR, and average position) directly into [cognee](https://github.com/topoteretes/cognee) memory.

```python
import cognee
from cognee_community_connector_google_search_console import google_search_console_source

# Ingest Search Console queries and pages into cognee
source = google_search_console_source(
    site_urls=["https://example.com/"],
    dimensions=["query", "page"],
    start_date="2026-09-01",
    end_date="2026-09-28",
)

await cognee.remember(source, dataset_name="search_console")

# Ask questions across your search performance graph
results = await cognee.search(
    query_text="Which queries drove the most organic clicks to our documentation?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["search_console"],
)
```

---

## Key Features

- **Document-Mode Ingestion**: Declares `cognee_document_source = "google_search_console"` via `DOCUMENT_SOURCE_ATTR`. Search analytics rows are converted into structured Markdown documents, allowing Cognee to extract entities (`Query`, `Landing Page`, `Domain`, `Metric`) and establish semantic graph edges.
- **Incremental Trailing-Window Sync**: Google Search Console data lags by ~2–3 days and recent data can be adjusted post-hoc. The connector tracks the last synced date in `dlt` resource state, re-queries a configurable trailing window (default 3 days) during incremental runs, and advances the cursor only when the full window is successfully processed.
- **Forget-on-Delete**: Built with full-snapshot support (`write_disposition="replace"`, matching Notion). When a property is removed upstream or deselected, it drops out of staging, and Cognee's `orphan_cleanup` purges its entities from graph and vector stores. In `merge` mode, explicit `_deleted=True` tombstones are emitted for removed properties.
- **Pagination & Volume Handling**: Search Console API caps responses at 25,000 rows per request. The connector automatically walks through result sets using `startRow` and `rowLimit` without loading whole tables into memory.
- **Resilient Network Handling**: Automatically retries rate limits (HTTP 429) and transient server errors (HTTP 5xx) with exponential backoff and `Retry-After` header respect.
- **Multiple Auth Methods**: Supports direct Bearer access tokens, OAuth 2.0 refresh flow (`client_id`, `client_secret`, `refresh_token`), downloaded `credentials.json` / `token.json` files, and Service Account tokens.

---

## Google Cloud & Search Console Setup

### 1. Enable the Search Console API
1. Visit the [Google Cloud Console](https://console.cloud.google.com/).
2. Create or select a Google Cloud Project.
3. Navigate to **APIs & Services > Library** and enable **Google Search Console API** (or Search Console API v3 / Webmasters API).

### 2. Configure Credentials
You can authenticate via **OAuth 2.0** or a **Service Account**:

#### Option A: OAuth 2.0 (User Account)
1. Go to **APIs & Services > Credentials** > **Create Credentials > OAuth client ID**.
2. Select **Desktop app** or **Web application**.
3. Download the client secrets JSON as `credentials.json`.
4. Ensure the OAuth scope `https://www.googleapis.com/auth/webmasters.readonly` is authorized.

#### Option B: Service Account (Server-to-Server)
1. Go to **APIs & Services > Credentials** > **Create Credentials > Service account**.
2. Copy the service account's email address (e.g., `gsc-reader@my-project.iam.gserviceaccount.com`).
3. Open [Google Search Console](https://search.google.com/search-console).
4. Go to **Settings > Users and permissions > Add user** for your property, paste the service account email, and grant **Read** permission.

---

## Installation

```bash
# Using uv
uv add "cognee-community-connector-google-search-console"

# Or pip
pip install "cognee-community-connector-google-search-console"
```

---

## Usage

### 1. Basic Ingestion (Auto-Discovery)
When `site_urls` is omitted, the connector discovers all verified properties accessible to the authenticated credentials:

```python
import cognee
from cognee_community_connector_google_search_console import google_search_console_source

source = google_search_console_source(
    token="ya29.a0AfH6SM...",  # or set GSC_ACCESS_TOKEN in env
    dimensions=["query", "page"],
)

await cognee.remember(source, dataset_name="search_console")
```

### 2. Incremental Sync with Trailing Window
On subsequent syncs, the connector re-evaluates the trailing window (e.g. 3 days back from last sync) to ingest delayed/revised performance numbers:

```python
source = google_search_console_source(
    token="ya29.a0AfH6SM...",
    site_urls=["https://example.com/"],
    trailing_days=3,
    write_disposition="replace",
)

await cognee.remember(source, dataset_name="search_console")
```

### 3. Querying the Knowledge Graph
```python
answer = await cognee.search(
    query_text="What are our top search queries and their landing pages?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["search_console"],
)
print(answer)
```

---

## Configuration Reference

| Parameter | Type | Default | Description |
|---|---|---|---|
| `token` | `str \| None` | `None` | Direct Bearer access token (falls back to `GSC_ACCESS_TOKEN`). |
| `client_id` | `str \| None` | `None` | OAuth2 Client ID for automated token refresh. |
| `client_secret` | `str \| None` | `None` | OAuth2 Client Secret for automated token refresh. |
| `refresh_token` | `str \| None` | `None` | OAuth2 Refresh Token for automated token refresh. |
| `credentials_path` | `str \| None` | `None` | Path to Google OAuth client secret JSON file. |
| `token_path` | `str \| None` | `None` | Path to cached OAuth token JSON file. |
| `site_urls` | `list[str] \| None` | `None` | Target properties. When `None`, discovers all verified properties. |
| `dimensions` | `list[str] \| None` | `["query", "page"]` | Search analytics grouping dimensions (`query`, `page`, `country`, `device`, `date`). |
| `start_date` | `str \| None` | `None` | Start date (YYYY-MM-DD). Defaults to 28 days prior to `end_date`. |
| `end_date` | `str \| None` | `None` | End date (YYYY-MM-DD). Defaults to 3 days ago (accounting for GSC lag). |
| `trailing_days` | `int` | `3` | Lookback days during incremental sync to ingest revised data. |
| `data_state` | `str` | `"final"` | `"final"` for finalized data, or `"all"` for fresh data. |
| `row_limit` | `int` | `1000` | Pagination batch size per request (max 25,000). |
| `max_rows_per_site` | `int \| None` | `None` | Maximum rows to ingest per site (useful for development limits). |
| `write_disposition` | `str` | `"replace"` | `dlt` disposition (`"replace"` for snapshot, or `"merge"`). |
| `client` | `Any` | `None` | Injected `GoogleSearchConsoleClient` for testing. |

---

## Testing

The test suite runs 100% offline using mocked HTTP transports (no live Google credentials needed):

```bash
uv --directory packages/connector/google-search-console run --with pytest pytest -v tests
```
