# cognee-community-connector-browser

A browser history & bookmarks data-source connector for [cognee](https://github.com/topoteretes/cognee): sync your local research trail and bookmarks into memory — *"what was that article about Postgres vacuum tuning I read last month?"*.

It exposes a `dlt` resource you hand directly to `cognee.remember(...)`, integrating seamlessly with cognee's DLT ingestion subsystem (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`). You get **incremental sync** (last-visit timestamp cursor), **forget-on-delete** (pages cleared or removed in the browser are purged from memory on the next sync), and **rich per-row node set scoping** (`browser:<browser>`, `browser:<domain>`, `browser:bookmarks/<folder>`).

---

## Key Features

- **Zero Auth / Local First**: Reads directly from your local browser profile on disk. No API keys, credentials, or cloud accounts needed.
- **Cross-Browser & Multi-Platform**:
  - **Chromium Family**: Google Chrome, Microsoft Edge, Brave, and Vivaldi (reads `History` SQLite database and `Bookmarks` JSON).
  - **Firefox**: Mozilla Firefox (reads `places.sqlite` history and bookmarks).
  - **Platforms**: Auto-detects profiles on Windows, macOS, and Linux, or accepts custom profile directories.
- **Concurrent Safe**: Safely copies SQLite databases and write-ahead logs (`-wal`, `-shm`) to a temporary staging area with integrity verification. Sync works while your browser is running without lock contention.
- **Incremental Sync**: Tracks last visit time across runs using browser-native epoch conversion (WebKit $\mu s$ since 1601 for Chromium, Unix $\mu s$ since 1970 for Firefox). Only visits since the last run are processed.
- **Forget-on-Delete**: Complete key scans detect vanished URLs or removed bookmarks and emit `_deleted` tombstones, purging them from graph, vector, and relational stores on `merge`.
- **Privacy by Design**: Built-in exclusion lists (auth/SSO, banking, webmail, search engines, localhost/RFC1918 IPs, internal schemes), automatic credential & query parameter redaction (`token`, `key`, `session`, `auth`), tracking parameter stripping, and off-by-default page content fetching.

---

## Installation

```bash
uv pip install cognee-community-connector-browser
# or, from this monorepo:
cd packages/connector/browser && uv sync --all-extras
```

---

## Quickstart

```python
import asyncio
from datetime import datetime, timedelta, timezone
import cognee
from cognee_community_connector_browser import browser_source

async def main():
    # Ingest history from the last 14 days for documentation sites
    source = browser_source(
        since=datetime.now(timezone.utc) - timedelta(days=14),
        include_domains=["docs.python.org", "github.com"],
        min_visits=1,
    )

    await cognee.remember(
        source,
        dataset_name="my_browser_trail",
        primary_key="id",
        write_disposition="merge",  # Incremental upsert & delete tracking
        max_rows_per_table=0,       # Full-corpus orphan cleanup
    )

    # Search your research trail
    results = await cognee.search(
        query_text="What libraries did I look up for PDF parsing?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["my_browser_trail"],
    )
    print(results)

if __name__ == "__main__":
    asyncio.run(main())
```

See [examples/example.py](file:///d:/Coding/open_source/cognee-community/packages/connector/browser/examples/example.py) for the complete runnable example.

---

## Configuration Options

The `browser_source(...)` factory supports the following parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `browser` | `str` | `None` | Browser to read: `"chrome"`, `"edge"`, `"brave"`, `"vivaldi"`, or `"firefox"`. If omitted, automatically detects default profile. |
| `profile_path` | `str \| Path` | `None` | Path to the browser profile directory (e.g. `~/.config/google-chrome/Default`). Overrides auto-detection. |
| `since` | `datetime \| int \| float` | `None` | Filter visits on or after this timestamp. Supports UTC `datetime`, Unix seconds, or milliseconds. |
| `include_domains` | `Sequence[str]` | `None` | Whitelist specific domains (e.g. `["docs.python.org", "github.com"]`). Subdomains are matched automatically. |
| `exclude_domains` | `Sequence[str]` | `None` | Exclude specific domains in addition to default privacy exclusions. |
| `min_visits` | `int` | `1` | Minimum visit count required for a URL to be ingested (helps filter one-off redirects and noise). |
| `bookmarks_only` | `bool` | `False` | When `True`, only ingests URLs saved in bookmarks (history visits for other URLs are skipped). |
| `fetch_content` | `bool` | `False` | When `True`, fetches readable web page body text via HTTP. Off by default for privacy and speed. |
| `http_timeout` | `float` | `10.0` | Timeout in seconds for optional page content fetches. |

---

## Privacy & Security

Browser history is sensitive personal data. This connector implements defense-in-depth protections:

1. **Off-by-Default Page Fetching**: By default (`fetch_content=False`), only titles, normalized URLs, domains, visit counts, timestamps, and bookmark folder paths are ingested. No web requests are made unless explicitly enabled.
2. **Default Exclusions**:
   - Internal browser schemes (`chrome://`, `edge://`, `brave://`, `about:`, `file:`, `blob:`, `data:`, `javascript:`, etc.).
   - Localhost and private intranet addresses (`localhost`, `127.0.0.1`, `::1`, `10.*`, `192.168.*`, `172.16-31.*`, `.local`, `.internal`).
   - Authentication, identity providers, and account management portals (`accounts.google.com`, `login.microsoftonline.com`, `auth0.com`, `okta.com`, `github.com/login`, etc.).
   - Online banking and financial institutions (`chase.com`, `bankofamerica.com`, `wellsfargo.com`, `paypal.com`, `stripe.com`, etc.).
   - Webmail services (`mail.google.com`, `outlook.live.com`, `mail.yahoo.com`, etc.).
   - Search result pages (`google.com/search`, `bing.com/search`, `duckduckgo.com/?q=`) and redirect hops.
3. **URL Redaction**:
   - Query parameters containing credentials or tokens are replaced with `[REDACTED]` (e.g. `token`, `access_token`, `auth`, `key`, `api_key`, `secret`, `password`, `code`, `session`, `sig`, `state`).
   - Tracking parameters are completely stripped (`utm_*`, `gclid`, `fbclid`, `mc_eid`, `ref`, etc.).
   - Basic authentication embedded in URLs (`https://user:pass@host/`) is scrubbed.
4. **Safe Database Reading**:
   - Live browser database files are **never opened directly**.
   - The connector copies `History` / `places.sqlite` and WAL logs into a temporary directory, runs a `PRAGMA integrity_check`, and queries the copy.
   - If copying fails (e.g. profile in use or permission denied), the sync safely aborts with an informative error rather than treating the failure as an empty history.

---

## How Incremental Sync & Forget-on-Delete Work

### 1. Incremental Sync (Cursor)
Each browser stores visit timestamps using its native epoch:
- **Chromium**: Microseconds since `1601-01-01 00:00:00 UTC` (WebKit epoch).
- **Firefox**: Microseconds since `1970-01-01 00:00:00 UTC` (Unix epoch).

The connector persists the maximum visit timestamp in `dlt`'s resource state under `resource_state["last_visit_time"]`. Subsequent runs only query visits recorded after this cursor, ensuring fast and lightweight updates.

### 2. Forget-on-Delete (Tombstoning)
Browsers do not provide a deletion feed for cleared history or removed bookmarks. On each run, the connector:
1. Conducts a fast ID scan of all surviving URLs in the profile matching your filter criteria.
2. Compares the current ID set against `resource_state["known_ids"]` from previous runs.
3. Emits hard-delete tombstones (`{"id": <url_id>, "_deleted": True}`) for any URLs that were deleted or excluded.
4. When loaded with `write_disposition="merge"`, `dlt` and cognee's `orphan_cleanup` permanently purge those pages and their embeddings from memory.

---

## Node Sets & Memory Scoping

Each ingested page is assigned node set tags in `cognee_node_set`:
- `browser:<browser_name>` (e.g. `browser:chrome`, `browser:firefox`)
- `browser:<domain>` (e.g. `browser:docs.python.org`, `browser:github.com`)
- `browser:bookmarks` (if bookmarked)
- `browser:bookmarks/<folder_path>` (e.g. `browser:bookmarks/Dev/Rust`)

This allows queries in cognee to be scoped cleanly:
```python
# Query only your Rust bookmarks
answer = await cognee.search(
    query_text="Error handling best practices",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    node_name=["browser:bookmarks/Rust"],
)

# Query documentation visited on python.org
answer = await cognee.search(
    query_text="contextlib asynccontextmanager usage",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    node_name=["browser:docs.python.org"],
)
```

---

## Testing

Run the comprehensive test suite:

```bash
uv run pytest packages/connector/browser/tests/ -v
```

The offline test suite uses isolated SQLite test fixtures to verify:
- Chromium WebKit and Firefox Unix epoch date conversions.
- URL normalization, query param redaction, and tracking parameter stripping.
- SSRF and private IP protection, default exclude lists, and search engine query suppression.
- Reading Chromium `History` + `Bookmarks` and Firefox `places.sqlite`.
- Locked database copy handling and integrity checks.
- Incremental sync cursors and forget-on-delete tombstones.
- Full end-to-end `dlt` merge pipeline execution into DuckDB confirming physical record deletion.
