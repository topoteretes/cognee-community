# cognee-community-connector-intercom

An Intercom data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your support history into memory — "what did we tell customers about double
charges, and what fixed it?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. It ingests:

- **Conversations**: one document each, with the opening message and every reply and
  internal note in order, labelled with the author's role and name (emails are left
  out). Internal notes stay attached to the conversation they explain.
- **Help Center articles**: published ones by default; drafts are opt-in.
- **Contact notes**: opt-in.

Documents go through cognee's normal cognify entity extraction (document mode), not
the deterministic dlt-row path.

## Requirements

Pins cognee 1.6.2. Document mode (`DOCUMENT_SOURCE_ATTR`) arrived in 1.4.0, and 1.6.x
adds `PIPELINE_SCOPE_ATTR`, which this source sets so its sync state (conversation
cursor and known ids) is kept per workspace region and cognee dataset. On 1.4.0 every
dlt source shares one pipeline state, so syncing a second dataset on the same machine
resets the cursor and the deletion sweep. Sync one workspace per cognee dataset.

## Install

From this monorepo:

```bash
cd packages/connector/intercom && uv sync
```

## Setup

1. Use your Intercom workspace, or create a free
   [development workspace](https://developers.intercom.com/docs/build-an-integration/getting-started/)
   from the Developer Hub.
2. In the Developer Hub, create an app on that workspace and copy its **access token**
   from the Authentication page. The connector only reads.
3. Export it, plus your `LLM_API_KEY` like any other cognee run:

```bash
export INTERCOM_ACCESS_TOKEN=...
export LLM_API_KEY=sk-...
```

For a workspace hosted in the EU or Australia, pass
`base_url="https://api.eu.intercom.io"` or `base_url="https://api.au.intercom.io"`.

## Usage

```python
import cognee
from cognee_community_connector_intercom import intercom_source

await cognee.remember(
    intercom_source(),  # INTERCOM_ACCESS_TOKEN from env, or token=...
    dataset_name="intercom",
    write_disposition="merge",  # required, see below
    self_improvement=False,  # keep repeat syncs cheap
)

answer = await cognee.search(
    query_text="How do we handle customers who were charged twice?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["intercom"],
)
```

Choose what to sync with `include_conversations`, `include_articles`,
`include_draft_articles` and `include_contact_notes`. See `examples/example.py` for a
runnable version.

## How sync and forget-on-delete work

Pass `write_disposition="merge"` to `remember`. cognee applies the caller's write
disposition to every table the source produces. Conversations sync incrementally, and
deletions are emitted as `_deleted` tombstone rows; both only work under `merge`.

- **Conversations** come from the search endpoint filtered on `updated_at`, starting
  five minutes before the newest `updated_at` seen on the previous run, because the
  search index can trail recent writes. The cursor is kept in dlt state, so unchanged
  conversations are not re-fetched.
- **Articles and contact notes** are re-read in full each run. Unchanged ones keep the
  same content hash and are not re-cognified.
- **Deletions.** Intercom has no delete feed. Each run therefore lists the ids that
  exist now and tombstones the ones that disappeared since the last run, and cognee's
  orphan cleanup forgets them. That covers deleted conversations, deleted or
  unpublished articles, and deleted contact notes.
- **Turning content off.** If you switch off a content type you synced before (for
  example `include_contact_notes`), its documents are tombstoned and forgotten on the
  next sync rather than left in memory.
- **Failures.** Rate limits (429) and server errors are retried, honouring
  `Retry-After` / `X-RateLimit-Reset`. Any other failed request aborts the run, so a
  broken listing never looks like a deletion.

**Limits:**

- Intercom returns at most the 500 most recent parts of a conversation. Longer ones are
  truncated, and this is logged and noted in the document.
- The deletion sweep lists every conversation id each run (150 per request).
- Contact notes take one request per contact, so keep them off for large workspaces.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake `requests` session (no workspace needed). They cover:

- rendering;
- cursor pagination;
- retries and backoff;
- the incremental search window;
- the deletion sweeps, including a conversation deleted mid-sync;
- article publishing state and opt-in contact notes;
- full dlt `merge` runs into a temporary SQLite destination (first sync, edits
  re-sync, deleted content is removed).
