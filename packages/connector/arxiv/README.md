# cognee-community-connector-arxiv

An arXiv data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync paper metadata and abstracts into memory — "ask my reading list".

It exposes a `dlt` source you hand to `cognee.remember(...)`, reusing cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`) — so you get
**incremental re-sync** (upsert by arXiv id, `merge` write disposition, `submittedDate` cursor)
and **forget-on-delete** (papers that leave the corpus are emitted as hard-deletes and purged
from memory on the next sync) with no core changes.

No account, no API key — the arXiv Atom API is public and read-only.

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
    arxiv_source(categories=["cs.AI", "cs.LG"], max_papers=200),
    dataset_name="papers",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by arXiv id
    max_rows_per_table=0,  # unlimited: orphan-cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="What approaches to agent memory show up in these papers?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["papers"],
)
```

Re-running `remember(...)` with the same dataset syncs only papers submitted since the last run
and forgets papers that left the corpus. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required** — the add pipeline defaults to `"replace"`,
> which would wipe the synced corpus on the second sync.

### Scoping the query

At least one of `categories`, `authors`, or `extra_query` is required; an unscoped source is
rejected rather than quietly trying to sync all of arXiv.

```python
arxiv_source(categories=["cs.CL"])  # a category
arxiv_source(authors=["Yoshua Bengio"])  # an author
arxiv_source(categories=["cs.AI"], authors=["Ada Lovelace"])  # both, AND-ed
arxiv_source(categories=["cs.AI"], extra_query='abs:"knowledge graph"')
```

Categories OR with each other, authors OR with each other, and the groups AND together.

## How sync + forget-on-delete work

**Incremental sync** uses the paper's submission timestamp (Atom `published`), narrowed
server-side with an arXiv `submittedDate:[<cursor> TO 999912312359]` range and walked in
ascending order. The cursor lives in dlt's per-resource state, so a re-run resumes where it
left off and re-embeds only the delta. Ascending order also makes a half-finished run safe:
it has only skipped papers strictly older than the stamp it recorded.

**Forget-on-delete**: arXiv has no deletion feed, so each run sweeps the ids currently matching
the scope and compares them to the previous run's ids (also in resource state). Papers that
vanished are emitted with the `_deleted` hard-delete marker, dlt drops them on `merge`, and
cognee's `orphan_cleanup` removes them from the graph + vector + relational stores. If the
sweep comes back empty while papers were previously known, deletion is **skipped** for that run
— an arXiv outage should not purge your corpus.

**Primary key** is the *version-stripped* arXiv id (`2608.09617`, not `2608.09617v2`). arXiv
mints a new version suffix on every revision, so keying on the raw id would ingest `v2` as a
second paper and orphan `v1` forever. The version is kept in its own column.

### Revisions

The cursor tracks *submission* date, so a revision to an already-ingested paper — which bumps
Atom `updated` but not `published` — is not picked up by the incremental pass. Pass
`track_revisions=True` to also re-emit papers whose `updated` stamp moved. It costs no extra
requests, because the deletion sweep already has those entries in hand.

## Rate limiting and cost

arXiv asks for roughly **one request every three seconds**. The connector enforces that delay
ahead of every request rather than discovering it through 429s, and retries transient failures
with exponential backoff on top.

That makes request count the thing to watch. Each pass costs one request per `page_size` papers
(default 100, max 1000), and by default there are two passes per run — incremental and sweep:

| Scope | Papers | Requests/run @ `page_size=100` | Wall clock |
|---|---|---|---|
| One author | ~600 | ~7 (sweep) + delta | ~20s |
| `cs.AI`, capped | 1,000 | ~20 | ~1 min |
| `cs.AI`, uncapped | ~197,000 | ~3,940 | ~3.3 hours |

So bound a broad category with `max_papers`, raise `page_size`, or pass
`detect_deletions=False` to skip the sweep — at the cost of never forgetting withdrawn papers.

### `max_papers` and forget-on-delete

A capped sweep walks the oldest `max_papers` submissions and stops, so it can only vouch for
that window. Deletion is therefore judged **inside the window only**: a known paper past the
cap was never examined and is left alone rather than treated as deleted. Without that bound,
`known_ids - current_ids` grows every run once the corpus outgrows the cap and starts purging
live papers from memory. Leave `max_papers=None` if you want forget-on-delete across the whole
scope.

## Setup

Nothing to configure for arXiv itself. You still need your `LLM_API_KEY` like any other
cognee run:

```bash
export LLM_API_KEY="sk-..."
uv run python examples/example.py
```

## Testing

```bash
uv run pytest tests/
```

The tests mock the arXiv Atom API (no network) and include an offline end-to-end run that
drives the source through a real `dlt` merge to prove a delete marker physically removes the
row — exactly what cognee's `orphan_cleanup` reconciles against.
