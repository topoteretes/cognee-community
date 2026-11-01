# cognee-community-connector-arxiv

An arXiv data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync paper metadata and abstracts into memory — "ask my reading list".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Papers
are rendered to markdown and ingested as **normal documents** (they flow through cognee's
cognify entity-extraction pipeline, not the deterministic dlt-row path), via cognee's
document-mode marker.

No account, no API key, no OAuth — arXiv's API is open.

## Install

```bash
uv pip install cognee-community-connector-arxiv
# or, from this monorepo:
cd packages/connector/arxiv && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_arxiv import arxiv_source

await cognee.remember(
    arxiv_source(categories=["cs.AI"], submitted_from="20260101"),
    dataset_name="arxiv",
)

answer = await cognee.search(
    query_text="What problems are these papers trying to solve?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["arxiv"],
)
```

### Scoping what you ingest

| Argument | Meaning |
|---|---|
| `categories=["cs.AI", "cs.CL"]` | arXiv categories, combined with OR |
| `authors=["Ada Lovelace"]` | Author names, combined with OR and ANDed with categories |
| `search_query='ti:"attention"'` | Raw arXiv query syntax; replaces the built clauses |
| `submitted_from` / `submitted_to` | `YYYYMMDD` bounds on submission date |
| `max_results=100` | Stop after this many papers |

At least one of these is **required**. Syncing all of arXiv is refused rather than
attempted.

## Rate limiting

arXiv asks for no more than one request every 3 seconds, and answers over-rate clients
with 403. The connector spaces its own requests accordingly — you do not need to add
delays around it. Sleeping only counts the time still owed, so parsing time is not added
on top. Over-rate (403), server (5xx), and network errors are retried with backoff that
never drops below arXiv's own minimum spacing.

## How sync + forget-on-delete work

The source is a **full snapshot of your query**: `write_disposition="replace"` rewrites
staging with exactly the papers matching on each run. This is what makes "incremental"
and "forget-on-delete" compatible rather than contradictory:

- **Forget-on-delete** falls out for free. A withdrawn or reclassified paper stops
  matching the query, so it is absent from the snapshot and cognee's existing
  `orphan_cleanup` removes it from the graph and vector stores. arXiv has no delete feed,
  so absence is the only available deletion signal.
- **Incremental** is handled by content hashing, not by a shrinking cursor. Unchanged
  papers produce a byte-identical row, keep a stable content-hash `data_id`, and are not
  re-ingested or re-cognified.

`submitted_from` / `submitted_to` therefore **bound the corpus**, and are deliberately not
advanced automatically between runs. A cursor that narrowed the query each run would be
actively wrong here: under `replace`, fetching only new papers would drop every
previously-synced paper out of the snapshot and cognee would forget the back catalogue.

Rows keep only `id`/`url`/`title`/`content`, and both identifiers are version-stripped, so
a new version that does not change the title or abstract does not churn the content hash.
A v2 whose abstract *did* change produces different content and is correctly re-ingested.

A fetch or parse error aborts the run (leaving memory untouched) rather than letting a
partial snapshot forget live papers.

## Testing

```bash
uv run pytest tests/
```

The tests need no network and no key — the fetcher is injected — and cover query
construction, Atom parsing, rate-limit spacing, retry classification, and full-snapshot
forget-on-delete (revision / removal on re-sync) through a real dlt pipeline into a temp
sqlite destination.
