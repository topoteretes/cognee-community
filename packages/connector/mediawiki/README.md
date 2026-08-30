# cognee-community-connector-mediawiki

A MediaWiki data-source connector for [cognee](https://github.com/topoteretes/cognee).
It turns selected wiki pages into normal cognee documents and keeps them current through
MediaWiki's `recentchanges` feed.

## Features

- Public wikis need no credentials; authenticated private wikis can inject an `httpx.Client`.
- Scope ingestion by exact page titles, a page-title prefix, and namespace IDs.
- Store page text plus latest revision and category metadata.
- Incrementally fetch only changed pages after the initial load.
- Propagate page deletions through DLT hard-delete rows so cognee can forget stale content.
- Parse wikitext to plain text with `mwparserfromhell`.

## Install

```bash
uv pip install cognee-community-connector-mediawiki
# or, from this monorepo:
cd packages/connector/mediawiki && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_mediawiki import mediawiki_source

source = mediawiki_source(
    api_url="https://www.mediawiki.org/w/api.php",
    page_prefix="API:",
    namespaces=[0],
)

await cognee.remember(source, dataset_name="mediawiki")

answer = await cognee.search(
    query_text="How does the MediaWiki Query API work?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["mediawiki"],
)
```

For a fixed set of pages, use exact titles instead:

```python
source = mediawiki_source(
    api_url="https://en.wikipedia.org/w/api.php",
    page_titles=["Retrieval-augmented generation", "Knowledge graph"],
)
```

At least one of `page_titles` or `page_prefix` is required. This prevents accidentally
starting an unbounded sync against a large public wiki. `MEDIAWIKI_API_URL` can replace the
`api_url` argument.

For an internal wiki, pass a preconfigured synchronous `httpx.Client` with the required
cookies, headers, TLS settings, or HTTP authentication through `client=`. The connector
never modifies the wiki.

## Sync behavior

The first run records the wiki server time before it lists and fetches the selected pages.
Later runs query `recentchanges` from the stored cursor and fetch only pages with edit, new,
restore, category, or in-scope move events. Delete log entries become DLT hard-delete rows.

MediaWiki documents that recent-change rows can arrive slightly out of timestamp order. The
connector therefore overlaps each query by 10 seconds and remembers recent `rcid` values so
late rows are processed while already-seen rows are not repeated. Cursor state is updated only
after a complete successful iteration; a failed page request does not advance it.

The `recentchanges` retention window is configured by each wiki. Re-sync before that window
expires, or run a fresh initial sync after a long outage.

## Testing

```bash
uv run --with pytest pytest tests/
```

Tests use a fake HTTP client, so no live wiki or API credentials are required.
