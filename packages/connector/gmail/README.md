# cognee-community-connector-gmail

A Gmail data-source connector for [cognee](https://github.com/topoteretes/cognee):
turn your inbox into memory — "ask my inbox".

It exposes a `dlt` source you hand straight to `cognee.remember(...)`, so it reuses
cognee's existing DLT ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` →
`orphan_cleanup`). That gives you **incremental re-sync** (Gmail `historyId` cursor,
`merge` write disposition) and **forget-on-delete** (messages you delete/trash in Gmail
are removed from memory on the next sync) with no parallel ingestion path and no changes
to core cognee.

## Install

```bash
uv pip install cognee-community-connector-gmail
# or, from this monorepo:
cd packages/connector/gmail && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_gmail import gmail_source

await cognee.remember(
    gmail_source(label_ids=["INBOX"]),
    dataset_name="gmail_inbox",
    primary_key="id",
    write_disposition="merge",  # REQUIRED — see note below
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="Summarize the most important emails in my inbox.",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["gmail_inbox"],
)
```

Re-running `remember(...)` with the same dataset syncs only the delta and forgets any
mail removed from Gmail. See `examples/example.py` for the full two-sync demo.

> **`write_disposition="merge"` is mandatory.** The add pipeline defaults to `"replace"`
> (drop + reload each run), which would wipe the whole synced inbox on the second sync.

## Setup (OAuth)

1. In Google Cloud Console: enable the Gmail API, configure an OAuth consent screen (add
   yourself as a test user), and create an **OAuth 2.0 Client ID → Desktop app**. Download
   the client-secret JSON.
2. Save it as `credentials.json` (or pass `credentials_path=...`). The first run opens a
   browser to consent and caches a token at `token.json` (written `0600`).
3. Set your `LLM_API_KEY` like any other cognee run.

Access is **read-only** (`gmail.readonly`) and strictly opt-in — nothing is fetched until
you construct a source and call `remember`. Scope what leaves your mailbox with
`label_ids`, keep `token.json` private, and use a dedicated dataset so you can wipe it with
a single `cognee.forget`.

## Testing

```bash
uv run pytest tests/
```

The tests fully mock the Gmail API (`FakeGmailService`) — no Google client libraries or
live credentials required — and include an offline end-to-end run that drives the source
through a real `dlt` merge to prove a delete marker physically removes the row (which is
exactly what cognee's `orphan_cleanup` reconciles against).
