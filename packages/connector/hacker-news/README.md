# cognee-community-connector-hacker-news

A Hacker News data-source connector for [cognee](https://github.com/topoteretes/cognee):
track topics you care about and sync matching stories + discussion threads into
memory — "ask what HN is saying".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`.
Stories are rendered to markdown and ingested as **normal documents** (they flow
through cognee's cognify entity-extraction pipeline, not the deterministic
dlt-row path), via cognee's document-mode marker.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the
> `resolve_dlt_sources` routing that reads it. **This is not in cognee 1.3.0.**
> The `cognee==` pin in `pyproject.toml` targets the first release that includes
> document-mode.

## Install

```bash
pip install cognee-community-connector-hacker-news
# or, from the cognee-community monorepo:
cd packages/connector/hacker-news && uv sync
```

No API key, no account, no OAuth — Hacker News discovery uses the public
[Algolia HN Search API](https://hn.algolia.com/api/v1) and thread hydration uses
the public [Firebase HN API](https://github.com/HackerNews/API).

## Usage

```python
import cognee
from cognee_community_connector_hacker_news import hacker_news_source

await cognee.remember(
    hacker_news_source(["AI agents", "rust"]),
    dataset_name="hacker-news",
)

answer = await cognee.search(
    query_text="What are people saying about AI agents?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["hacker-news"],
)
```

See `examples/example.py` for the full runnable flow.

## Configuration

```python
hacker_news_source(
    topics=["AI agents", "rust"],  # required: what to track
    max_stories_per_topic=25,  # stories fetched per topic per run
    max_comments_per_story=10,  # comments hydrated per story (all depths)
    comment_depth=1,  # 1 = top-level comments only
    since_days=30,  # length of the tracked window
)
```

### Topic examples

Topics are plain search phrases matched against story titles/URLs by Algolia:

- `["AI agents"]` — agent frameworks, evals, memory
- `["rust"]` — the language and its ecosystem
- `["Ask HN"]` — Ask HN threads
- `["Show HN"]` — Show HN launches

At least one non-empty topic is required — selecting topics is how you choose
what gets ingested (there is no auth step).

## How incremental sync works

Each run snapshots the **tracked window** (`since_days`, default 30 days) per
topic with `write_disposition="replace"`. Two things make this incremental in
effect:

1. **Stable content-hash ids.** A story's document content is built only from
   stable fields (title, text, comments) — volatile counters like points are
   excluded. Unchanged stories therefore keep a byte-identical `data_id` and
   cognee does not re-ingest or re-cognify them.
2. **A per-topic cursor** (`max_created_at_i`) is persisted in dlt state, so
   every run records its high-water mark and new stories are picked up on the
   next sync.

Stories that age out of the window are out of scope and fall out of the
snapshot (see deletion behavior below).

## How forget-on-delete works

The snapshot is authoritative: it contains exactly the stories currently
matching your topics upstream. A story that was deleted (or turned dead), or
that simply no longer matches, is absent from the snapshot — and cognee's
existing `orphan_cleanup` then removes its document from the graph and vector
stores on the next sync.

Safety rule: transient API failures are retried and then **abort the run**
rather than producing a partial snapshot. Under `replace`, a partial snapshot
would wrongly forget live stories, so failing loudly is the correct behavior.
Use a dedicated dataset per topic set so a `cognee.prune` can wipe it cleanly.

## Tests

```bash
cd packages/connector/hacker-news
pytest tests/ -v
```

All tests are deterministic — a fake HTTP client stands in for the Algolia and
Firebase APIs, and dlt pipeline tests run against a temp sqlite destination.
