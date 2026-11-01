# cognee-community-connector-xkcd

An xkcd data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync the xkcd archive into memory — "ask xkcd".

It exposes a `dlt` resource you hand to `cognee.remember(...)` / `cognee.add(...)`.
Each comic is rendered to a text document (title, publication date, image link, alt
text, and transcript) and ingested as a **normal document** (it flows through cognee's
cognify entity-extraction pipeline, not the deterministic dlt-row path), via cognee's
document-mode marker. No credentials are required — the xkcd JSON API is public.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it. Document-mode first shipped in cognee 1.4.0; this package
> pins and is tested against cognee 1.6.2.

## Install

```bash
uv pip install cognee-community-connector-xkcd
# or, from this monorepo:
cd packages/connector/xkcd && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_xkcd import xkcd_source

await cognee.remember(
    xkcd_source(),  # no auth — the xkcd JSON API is public
    dataset_name="xkcd",
    write_disposition="merge",  # incremental upsert by comic id
    max_rows_per_table=0,  # reconcile the whole corpus (no read-back cap)
)

answer = await cognee.search(
    query_text="Which comics mention woodpeckers?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["xkcd"],
)
```

The first run backfills the whole archive (paced — see below); every later run fetches
only comics published since the stored watermark. Pass `since_num=<n>` to bound the
first backfill for a quick trial (e.g. `xkcd_source(since_num=3300)` fetches only the
last few comics).

## How sync + forget-on-delete work

The resource is **incremental**: a `dlt` cursor on the comic number keeps the watermark
in dlt's per-resource state, so re-running `remember` resumes where the last sync
stopped. Rows are merged by comic `id`, so already-ingested comics keep a stable
content-hash `data_id` and are not re-ingested or re-cognified.

xkcd has **no delete feed** — comics are immutable once published and the archive is
append-only — so the connector emits no hard-delete tombstones, and `write_disposition`
must be `"merge"` (a `"replace"` run would rewrite staging with only the new delta and
drop the rest of the corpus). A comic removed upstream mid-archive would therefore not be
detected (merge keeps its row), which is acceptable for an immutable corpus. The only
structural anomaly the connector can catch is the upstream latest comic number moving
behind the stored watermark: it logs a warning and neither fetches nor deletes anything.
No-op re-syncs load no rows, so cognee's `orphan_cleanup` has no evidence to act on.

## Politeness

Requests are paced by `min_interval` seconds (default 0.5) and transient failures
(429 / 5xx / timeouts / network errors) are retried with backoff, honoring
`Retry-After`. Pass `client=XkcdClient(min_interval=...)` to tune pacing, point at a
mirror via `XkcdClient(base_url=...)`, or share one connection pool across runs:

```python
from cognee_community_connector_xkcd import XkcdClient, xkcd_source

with XkcdClient(min_interval=1.0) as client:
    await cognee.remember(
        xkcd_source(client=client),
        dataset_name="xkcd",
        write_disposition="merge",
        max_rows_per_table=0,
    )
```

## Testing

```bash
uv run pytest tests/
```

The tests mock the xkcd API (no network) and cover comic rendering, the #404 gap,
retry/pacing behavior, and incremental re-sync (backfill, delta-only fetch, merge
keeps the corpus). They require a cognee build that includes document-mode (see
**Requirements**).
