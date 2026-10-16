# cognee-community-connector-rss

An RSS / Atom feed data-source connector for [cognee](https://github.com/topoteretes/cognee):
subscribe to blogs, changelogs, newsletters, and release feeds — "ask my feeds".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Feed
entries are cleaned to plain text and ingested as **normal documents** (they flow through
cognee's cognify entity-extraction pipeline, not the deterministic dlt-row path), via
cognee's document-mode marker.

## Install

```bash
uv pip install cognee-community-connector-rss
# or, from this monorepo:
cd packages/connector/rss && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_rss import rss_source

await cognee.remember(
    rss_source(
        feed_urls=[
            "https://example.com/feed.xml",  # RSS 2.0
            "https://example.org/atom.xml",  # Atom
        ]
    ),
    dataset_name="feeds",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by entry id
    max_rows_per_table=0,  # unlimited read-back so deletions reconcile fully
)

answer = await cognee.search(
    query_text="What was announced recently?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["feeds"],
)
```

See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

**Auth: none.** Any RSS 2.0, RSS 1.0/RDF, or Atom URL works (parsing is handled by
`feedparser`, which recovers from real-world malformed documents).

**Incremental sync** follows the entry's `updated` / `published` timestamp: each run
re-fetches every configured feed and emits only entries whose timestamp is newer than
what the previous run stored (per-entry state lives in dlt's per-resource state, so
re-running `remember` resumes where it left off). Feeds that carry no usable timestamps
fall back to a content-hash comparison, so edits are still picked up. Unchanged entries
are not re-emitted — and even when they are, cognee keeps their content-hash `data_id`
stable, so they are not re-ingested or re-cognified.

**Forget-on-delete:** feeds have no deletion feed — a deleted entry simply disappears
from the feed document. When an entry that was known on the previous run is missing from
a successfully fetched feed, the connector emits an `_deleted` hard-delete marker; dlt
removes that row on merge, and cognee's existing `orphan_cleanup` purges it from the
graph, vector, and relational stores. Removing a feed URL from `feed_urls` tombstones
that feed's entries on the next sync.

**Failure posture:** a feed that fails to fetch or parses to zero entries is skipped for
the run (with a warning) — its entries are never tombstoned on unseen evidence, so a
transient outage cannot mass-delete a feed's memory.

## Setup

No credentials are needed. Only your `LLM_API_KEY` (or keyless cognee setup), like any
other cognee run.

## Limitations

- Feeds that window their archive (e.g. "latest 20 items only") treat entries scrolling
  off the feed as upstream deletions — that is the only deletion signal RSS has, and it
  matches the snapshot semantics of the Notion connector.
- Syncing several *different* feed sets into one dataset? Give each `rss_source(...)`
  call its own `resource_name` so each set keeps its own incremental state and staging
  table.
- HTML in entries is stripped to plain text; media enclosures are ignored.

## Testing

```bash
uv run pytest tests/
```

The tests mock every feed (in-memory XML, no network) and cover RSS and Atom parsing,
the incremental timestamp cursor with content-hash fallback, tombstones for vanished
entries and removed feed URLs, malformed/empty/failing feed handling, and an end-to-end
forget-on-delete through a real dlt merge.
