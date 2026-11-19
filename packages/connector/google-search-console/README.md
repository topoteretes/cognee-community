# cognee-community-connector-google-search-console

A Google Search Console data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync how your sites perform in Google Search (queries, pages, clicks, impressions, CTR, position)
into memory, and forget a property when it is removed upstream.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Search Analytics is a table of
numbers, and one row per date, query and page would be millions of documents, so the connector
groups it by calendar month. Each month of a property becomes:

- an **overview**: totals, devices, top countries, top queries and top pages;
- one **page document** per top page, with the queries that led to it.

All of a property's documents share a node set (`search-console:<property>`), so cognify links
them: the same query or page in two months is one entity, and you can ask how it changed.

## Requirements

- cognee **1.6.4** or later (`>=1.6.4,<2`). The connector relies on document mode with
  table-scoped cleanup and per-row node sets (`DOCUMENT_SYNC_VERSION` 2) and checks for it when
  the source is built.
- Python 3.10 to 3.14.
- A Google account with a verified Search Console property (see [Setup](#setup)).

## Install

```bash
uv pip install cognee-community-connector-google-search-console
# or, from this monorepo:
cd packages/connector/google-search-console && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_google_search_console import (
    google_search_console_source,
    list_properties,
)

print(list_properties())  # the properties you can select, and your permission on each

source = google_search_console_source(
    sites=["sc-domain:example.com"],  # omit for every verified property
    months=3,  # this month and the two before it
)  # credentials come from GOOGLE_SEARCH_CONSOLE_* (see Setup)

await cognee.remember(
    source,
    dataset_name="search_console",
    primary_key="id",
    write_disposition="merge",  # required: incremental runs emit only changes and deletions
)

answer = await cognee.recall(
    "Which queries bring clicks to our pricing page?", datasets=["search_console"]
)
```

Run the same `remember` call again whenever you want to sync. See `examples/example.py` for the
full flow.

| Argument | Meaning |
|---|---|
| `sites` | Properties to sync, exactly as `list_properties()` shows them (`sc-domain:example.com` or `https://example.com/`). Default: every verified property the account can see. |
| `months` | Calendar months to sync, counting the current one (1 to 16, the retention of Search Console). Default `3`. |
| `search_type` | `web` (default), `image`, `video`, `news`, `discover` or `googleNews`. Give each type its own `resource_name`. |
| `top_queries` | Queries listed in a month's overview. Default `25`. |
| `top_pages` | Pages per month that get a document. Default `25`; `0` for none. |
| `queries_per_page` | Queries listed in a page document. Default `10`; `0` for none. |
| `full_refresh` | Read every month in the window again, for example after changing how documents are rendered. |
| `resource_name` | Name for this connection's dlt state and staging tables. Default `google_search_console`; give each Google account its own when several sync into one dataset. |
| `access_token`, `settings` | Credentials, see [Setup](#setup). |
| `client` | A `SearchConsoleClient`, for a custom base URL, transport or retry behaviour. `service` is an alias. |
| `check_active` | Called before every request; raises to stop a sync whose access was revoked (for hosts). |

After a run, `source.cognee_sync_stats` holds counts only (sites, periods, closed, documents,
unchanged, deleted, kept, failed_rate_limit), never queries or pages.

## Setup

### 1. Create an OAuth client

1. In the [Google Cloud Console](https://console.cloud.google.com/), create or pick a project
   and enable the **Google Search Console API**.
2. Configure the **OAuth consent screen**. For your own use, an *External* app left in
   *Testing* with your Google account added as a test user is enough. Add the scope
   `https://www.googleapis.com/auth/webmasters.readonly`. A *Testing* app's refresh token expires
   after 7 days, so sign in again with `login.py`; publish the app to avoid that.
3. Under *Credentials*, create an **OAuth client ID** of type **Desktop app** and download its
   JSON.

The connector only ever reads, and only asks for the read-only scope.

### 2. Sign in

Pick one:

| How | Settings |
|---|---|
| **Browser, once** (local) | `GOOGLE_SEARCH_CONSOLE_CREDENTIALS_PATH=~/client_secret.json`, then `uv run python examples/login.py`. The token is cached, mode 0600, at `GOOGLE_SEARCH_CONSOLE_TOKEN_PATH` (default `~/.cognee_google_search_console_token.json`) and renewed on its own. |
| **Refresh token** (headless) | `GOOGLE_SEARCH_CONSOLE_REFRESH_TOKEN`, `GOOGLE_SEARCH_CONSOLE_CLIENT_ID`, `GOOGLE_SEARCH_CONSOLE_CLIENT_SECRET`. |
| **Access token** (a host, or a quick try) | `GOOGLE_SEARCH_CONSOLE_ACCESS_TOKEN`, or `access_token=`. It lasts about an hour and is never renewed. |

Tokens are sent only to Google, and are never logged or included in an error message.

### 3. Choose what to sync

By default, every verified property the account can see. Narrow it with `sites=[...]`. A
property in `sites` that is not found, or that is not verified for the account, raises an error
instead of being skipped, because skipping it would forget what it synced before. Remove it from
the list to forget its content on purpose. A property where your permission level is
*Unverified* is listed but cannot be read, and is never synced.

## How a sync works

- **Final data only.** Search Console publishes final data about two days late, and its newest
  days are preliminary and sampled. The connector reads `dataState="final"` only, so the open
  month ends where Google has published it (its dates are Pacific time).
- **Incremental sync.** A month is *open* until it is a few days older than Google's publishing
  lag, because the last days of a month can still be revised. An open month is read again on
  every run (six requests: totals, devices, countries, queries, pages, and page + query rows);
  the first run after that reads it one last time and *closes* it, and a closed month is never
  read again. Only documents whose text changed are sent on, so an unchanged run makes no LLM call
  and the open month re-sends just what moved. Changing `top_pages`, `top_queries`,
  `queries_per_page` or `search_type` re-reads the closed months once.
- **Forget-on-delete.** A property that is removed from your account, loses its verification, or
  is taken out of `sites` is removed from the graph and vector stores on the next sync. A page
  that falls out of an open month's top pages loses its document for that month.
- **Errors and sampling never delete.** A failed request stops the run before anything is
  committed, so nothing is forgotten and the next run starts from the same state. Google returns
  only the top rows by clicks and withholds rare queries, so a query that is absent is never read
  as deleted: closed months are not re-read, and a month that suddenly returns no data keeps its
  documents. A property missing from the account's listing is looked up by name before it is
  forgotten, and an account that sees no verified property stops the run instead of forgetting
  everything.
- **Months are only added.** A month that leaves the `months` window, or ages out of Google's
  16 months, stays in memory until its property is forgotten.
- **Rate limits.** A 429 (or a `rateLimitExceeded` 403) is retried after `Retry-After`, and 5xx and
  network errors with backoff. One wait is capped at 60 seconds and a run waits 5 minutes at
  most, because a sync holds cognee's staging lock. Past that the run stops cleanly: the months
  read so far are committed, nothing is forgotten, `failed_rate_limit` is `1` in the stats, and
  the next run resumes with the rest. A property with a lot of traffic reads up to 50,000
  page + query rows per month (two requests), the top ones by clicks.
- **Hosts.** `check_active` is called before every request, not only between rows, so a revoked
  connection stops a sync in the middle of a month.

## Privacy

This reads your sites' search queries and pages and sends them, as text, to your configured LLM
and embedding providers during cognify. Select only the properties you mean to and use a
dedicated dataset so you can forget it in one call. Query strings come from the public web, so
the connector renders each on one short line.

## Limitations

- The graph is **not an accounting dataset**. The rows listed in a document are the top rows by
  clicks, and Google leaves rare queries out for privacy, so they do not add up to the totals.
  The totals are Google's own and include them.
- Page documents cover each month's top pages by clicks (`top_pages`), and their queries are the
  top page + query rows by clicks. A long tail of low-click pages is in the totals only.
- Only the *final* data is synced: the last two days or so of a month arrive on a later run.
- Sitemaps, URL inspection and daily series are not ingested.
- Changes are detected by polling. Run the sync as often as you need it.

## Tests

```bash
cd packages/connector/google-search-console
uv sync
uv run pytest
```

No Google account or API key is needed. The tests run against an in-memory fake of the Search
Console API and Google's token endpoint, served through `httpx`:

- **Auth and client:** the three credential sources, token renewal, retries, `Retry-After`,
  percent-encoded property URLs and `startRow` paging.
- **Sync logic:** ingest, final-data cut-off, closed months, the trailing window and late
  revisions, option changes, and forget-on-delete for deselected, removed and unverified
  properties, plus the rule that a failed, empty or sampled read changes nothing.
- **dlt:** a real dlt pipeline checks that tombstones remove staged rows and that state survives
  between runs.
- **End-to-end:** the real cognee pipeline (with a mocked LLM and embeddings) checks that
  deselected and removed properties leave the graph, and that an unchanged re-sync makes no LLM
  calls.

To try it against a real property, run `examples/login.py` and then `examples/example.py`.
