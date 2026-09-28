# cognee-community-connector-rss

An RSS / Atom data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync any feed — blogs, changelogs, newsletters, release notes — into memory.

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Feed
entries are rendered to plain text and ingested as **normal documents** (they flow through
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
    rss_source(["https://hnrss.org/frontpage"]),  # or set RSS_FEED_URLS
    dataset_name="rss",
)

answer = await cognee.search(
    query_text="What are the top stories about?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["rss"],
)
```

Pass one URL or a list; a single string is accepted too. Omit the argument to read
`RSS_FEED_URLS` (comma- or whitespace-separated). **No auth** is required. See
`examples/example.py` for the full flow.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging with
exactly the entries currently present in the configured feeds on each run.

- **Incremental (no cursor):** unchanged entries keep a stable content-hash `data_id`, so
  they are not re-ingested or re-cognified — only new or edited entries do work. Identity is
  the entry `<guid>` / Atom `<id>` (falling back to the link); the row keeps only
  `id`/`url`/`title`/`content`, so a re-stamped `published` with identical text does not
  churn the id.
- **Forget-on-delete:** an entry dropped upstream is absent from the snapshot, so cognee's
  existing `orphan_cleanup` removes it from the graph and vector stores.
- **Rolling-window caveat:** most feeds only expose the newest N entries, so an entry that
  ages out of the feed is reconciled out of memory too. Point the connector at a
  full-archive feed, or use a dedicated dataset, if you need older entries retained.
- **Safe failure:** a fetch or parse failure aborts the run (leaving memory untouched)
  rather than letting a partial/empty snapshot forget live entries. Transient errors
  (429 / 5xx / timeout / network) are retried with backoff.

Both RSS and Atom are supported, and malformed feeds are parsed leniently (feedparser): a
recognized feed that still yields entries is used with a warning; a malformed feed that
yields nothing is treated as a failed fetch. Empty response bodies and HTML/XML error pages
also abort the snapshot, even if the server returns HTTP 200. Valid empty RSS/Atom feeds
remain accepted, so intentional upstream deletions can still be reconciled.

## Testing

```bash
uv run pytest tests/
```

The tests use in-memory feed fixtures (no network) and cover entry parsing (RSS + Atom),
HTML-to-text rendering, malformed-feed handling, the document-source marker, and
full-snapshot forget-on-delete (edit / remove on re-sync).
