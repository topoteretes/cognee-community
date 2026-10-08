# cognee-community-connector-metabase

Sync Metabase collections, saved questions, and dashboards into
[cognee](https://github.com/topoteretes/cognee) as normal documents. Questions
include their saved SQL/native query or query-builder definition; dashboards
include their cards and text cards. Queries are never executed and result data
is never downloaded.

## Install and setup

Requires Python 3.11–3.13 and cognee 1.4.0's document-mode ingestion (pinned in
`pyproject.toml`), following the Notion connector.

```bash
cd packages/connector/metabase
pip install -e .
```

1. Set up your self-hosted Metabase instance and create its initial admin user
   in the setup wizard (or sign in with an existing administrator).
2. On Metabase 0.49+, open **Admin settings → Settings → Authentication → API
   keys**, create a key, and assign a group with read access to the desired
   collections. See [Metabase API keys](https://www.metabase.com/docs/latest/people-and-groups/api-keys).
3. Export the instance URL, key, and your cognee LLM credentials:

   ```bash
   export METABASE_URL="https://metabase.example.com"
   export METABASE_API_KEY="mb_..."
   export LLM_API_KEY="..."
   python examples/example.py
   ```

For older instances or session authentication, omit `METABASE_API_KEY` and set
`METABASE_USERNAME` and `METABASE_PASSWORD`. The connector logs in with
`POST /api/session`, sends the returned `id` in `X-Metabase-Session`, and logs
out after the sync. API keys take precedence and use the `x-api-key` header;
invalid keys fail explicitly rather than silently changing identity.

## Usage

```python
import cognee
from cognee_community_connector_metabase import metabase_source

await cognee.remember(
    metabase_source(),  # credentials and URL from environment
    dataset_name="metabase",
)
```

Arguments `base_url`, `api_key`, `username`, and `password` override environment
values. Use `kinds=["card", "dashboard"]` to select content types; the default
also includes `collection`. All items visible to the credentials are listed.
Use Metabase group permissions to restrict collections. Instance URLs may
include a reverse-proxy path, such as `https://example.com/metabase`.

Use a dedicated cognee dataset per instance and selection. Keep the same
pipeline storage between runs. This example reads your saved definitions and
sends document content through your configured cognee ingestion/LLM pipeline.

## Incremental sync and deletion

Each sync fully lists `/api/collection`, `/api/card`, and `/api/dashboard` for
the selected types. Persisted per-item `updated_at` watermarks determine which
items need rendering/detail requests. A maximum cursor is also saved in dlt
source state. Newly visible IDs are ingested even with older timestamps.
Missing timestamps cause re-rendering, so older collection representations do
not lose edits. An unchanged sync raises `MetabaseUnchanged` to abort the
replace load safely (an empty iterator would truncate staging). dlt/cognee
wrap this signal in their extraction exceptions; `examples/example.py` shows
how to recognize it through `__cause__` and continue searching existing data.

Like Notion, staging uses `write_disposition="replace"`. Cached unchanged rows
remain in the complete snapshot; their stable content hashes avoid re-ingestion
and re-cognification. Deleted, archived, or no-longer-visible IDs disappear,
and cognee's deferred `orphan_cleanup` purges their graph/vector records.
A stable **Metabase source** document remains even when all upstream items are
deleted: cognee 1.4.0 skips cleanup for completely empty snapshots. This document
allows the final deletion to be reconciled through the same cleanup path.

All reads must succeed before state or staging is replaced. API failures abort
rather than treating an incomplete listing as deletion. The cached document
text lives in dlt state as well as staging; protect that storage like the corpus.
Dashboard embedded card details refresh when the dashboard timestamp changes;
saved questions independently refresh on their own timestamp.

Requests run sequentially with 100 ms pacing (configurable via
`request_interval`) and bounded backoff for 429, transient server errors, and
transport failures. Numeric `Retry-After` is honored. Session mode creates one
session per run and releases it, avoiding accumulation toward the default
50-session limit.

## Testing

```bash
pip install pytest
pytest tests/
```

Tests use fixture-based `httpx.MockTransport` and temporary SQLite dlt
pipelines, with sockets blocked. They cover both auth modes, rendering,
persisted incremental cursors, no-op sync, partial failures, pagination,
rate limits, and upstream deletions (including all items deleted). Cleanup
tests run cognee's document resolver and orphan cleanup with mocked graph and
persistence boundaries; they require no live server or LLM credentials.
