# cognee-community-connector-substack

A Substack data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync a newsletter into memory - "ask my newsletter". It reads the publication's public
RSS feed (**no authentication**) and exposes a `dlt` source you hand to
`cognee.remember(...)` / `cognee.add(...)`. Posts are rendered to markdown and ingested as
**normal documents** (they flow through cognify entity extraction) via cognee's
document-mode marker.

## Requirements

Requires a cognee release that ships document-mode (`DOCUMENT_SOURCE_ATTR` +
`resolve_dlt_sources`), i.e. cognee >= 1.4.0.

## Install

```bash
uv pip install cognee-community-connector-substack
# or, from this monorepo:
cd packages/connector/substack && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_substack import substack_source

await cognee.remember(substack_source("platformer"), dataset_name="substack")

answer = await cognee.search(
    query_text="What did they write about recently?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["substack"],
)
```

`publication` accepts a name (`"platformer"`), a host (`"platformer.news"`, for custom
domains) or a full feed URL. See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging with
exactly the posts currently in the feed on each run. RSS has no delete signal - an
unpublished post just disappears - so a deleted post drops out of the snapshot and
cognee's existing `orphan_cleanup` removes it from the graph and vector stores. Unchanged
posts keep a stable content-hash `data_id`, so they are not re-ingested or re-cognified;
only new and edited posts are processed (this is the incremental behaviour).

Safety: a fetch/parse failure, or a feed containing zero posts, aborts the run and leaves
memory untouched, rather than letting a partial or empty snapshot forget live posts.

### Paywalled posts

For paid posts the feed only carries a preview. These rows get `is_partial = true` and a
visible "Preview only" note in the content, so a clipped post is never mistaken for the
full text. Detection is a heuristic (missing `content:encoded`, or paywall phrases in the
body), since RSS has no official truncation flag.

### Limitations

- Substack's feed exposes only the most recent posts (typically ~20). A post that rolls
  out of that window looks identical to a deleted one and is forgotten on the next sync.
- Only RSS-visible content is ingested; subscriber-only full text is not accessible
  without authentication.

## Testing

```bash
uv run pytest tests/
```

Tests inject feed XML (no network) and cover URL resolution, HTML rendering, parsing,
paywall flagging, retry/backoff, and full-snapshot ingest / edit / new-post /
forget-on-delete behaviour through a real dlt pipeline.
