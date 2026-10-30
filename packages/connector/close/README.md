# cognee-community-connector-close

A Close CRM data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Close workspace into memory — "ask my CRM".

It exposes a `dlt` resource you hand to `cognee.remember(...)` / `cognee.add(...)`. Close
objects (leads, opportunities, notes, calls, emails) are rendered to plain text and
ingested as **normal documents** (they flow through cognee's cognify entity-extraction
pipeline, not the deterministic dlt-row path), via cognee's document-mode marker.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR`, `DOCUMENT_SYNC_VERSION ≥ 1`,
> and the `resolve_dlt_sources` routing that reads it.

## Install

```bash
uv pip install cognee-community-connector-close
# or, from this monorepo:
cd packages/connector/close && uv sync --all-extras
```

## Setup

1. **Get your Close API key**: Go to Close → Settings → API Keys → create a new key.
   The key only needs **read** permissions.
2. **Export it**:
   ```bash
   export CLOSE_API_KEY="api_..."
   export LLM_API_KEY="sk-..."   # your LLM provider key for cognify
   ```

## Usage

```python
import cognee
from cognee_community_connector_close import close_source

await cognee.remember(
    close_source(),  # CLOSE_API_KEY from env, or pass api_key=...
    dataset_name="sales_memory",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)

answer = await cognee.search(
    query_text="What objections came up in the latest call with Acme?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["sales_memory"],
)
```

### Selecting what to ingest

By default, **leads**, **opportunities**, **notes**, and **calls** are ingested.
To include emails or restrict the scope:

```python
source = close_source(selected_types=["lead", "call", "email"])
```

Available types: `lead`, `opportunity`, `note`, `call`, `email`.

### Multiple organizations

Give each Close organization its own `resource_name` so their incremental state
stays separate:

```python
source_acme = close_source(api_key=acme_key, resource_name="close_acme")
source_globex = close_source(api_key=globex_key, resource_name="close_globex")
```

## How incremental sync works

| Run | What happens |
|-----|-------------|
| **First** | Full read of every selected object type. Each object is rendered to text, content-hashed, and stored in dlt's per-resource state. All objects are yielded. |
| **Subsequent** | Full read again, but only objects whose content hash changed are yielded. Unchanged objects are skipped (stable `data_id` → no re-ingestion / re-cognify). |

## How forget-on-delete works

Each sync diffs the current set of object ids against the previous state. Any id
that was previously known but is now absent from the Close API becomes a `_deleted`
tombstone. dlt's `merge` + `hard_delete` drops it from the destination, and cognee's
`orphan_cleanup` purges it from the graph and vector stores.

**Safety**: a partial or failed API listing aborts the entire run before any tombstone
is emitted, so transient errors never cause accidental forgetting.

## Call summaries and transcripts

Close does not return call transcripts by default — they must be explicitly requested
via the `_fields` parameter. This connector handles that automatically:

- `summary` — the call summary text (the most valuable field)
- `recording_transcript` — full call transcript (truncated to 10 KB per document)
- `voicemail_transcript` — voicemail transcription

Calls without a summary or transcript are still ingested with their metadata
(direction, duration, date).

## Limitations

- The Close Event Log (30-day retention) is **not** used for change discovery.
  Every sync is a full reconciliation. An Event-Log-assisted optimization can be
  added as a follow-up to reduce API calls.
- Attachments and custom fields beyond the default set are not ingested.

## Testing

```bash
uv run pytest tests/
```

Tests mock the Close API via `httpx.MockTransport` (no live credentials needed)
and cover rendering, pagination, retry, incremental sync, and forget-on-delete.

## See also

- `examples/example.py` — runnable demo
- `packages/connector/notion/` — the closest existing connector reference