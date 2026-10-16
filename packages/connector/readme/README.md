# cognee-community-connector-readme

Sync one [ReadMe](https://readme.com/) documentation branch into
[cognee](https://github.com/topoteretes/cognee) memory. The connector reads
guide categories and guide pages, and can also include ReadMe's versionless
changelog. It only issues authenticated `GET` requests.

## Install

```bash
uv pip install cognee-community-connector-readme
# or from this repository
cd packages/connector/readme
uv sync --all-extras
```

## Setup and use

1. Create a ReadMe API v2 key with access to the project you want to read.
2. Export it as `README_API_KEY`.
3. Select exactly one documentation branch. Use `stable` for production, or a
   named preview branch to index that version instead.

```python
import cognee
from cognee_community_connector_readme import readme_source

source = readme_source(
    branch="stable",
    category_titles=["Getting started", "Authentication"],  # or None for all guides
    include_changelog=True,
)

await cognee.remember(
    source,
    dataset_name="product_docs",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```

`max_rows_per_table=0` is important for a full corpus: it prevents the
ingestion layer from truncating the destination before orphan cleanup runs.
See `examples/example.py` for a runnable example.

## Sync behavior

- **Authentication:** ReadMe API v2 Bearer authentication. Pass `api_key=` or
  set `README_API_KEY`; the key is never written into state or logs.
- **One version at a time:** `branch` defaults to `stable`. Record IDs include
  the selected branch, and the source never enumerates all branches, preventing
  the same page from becoming duplicate cross-version memories.
- **Incremental bodies:** Every run lists categories and pages to obtain
  `updated_at`; only new/changed guides have their full markdown body fetched
  and emitted. Per-resource revisions are stored in dlt resource state.
- **Forget on delete:** The same listing is a current-ID sweep. If a guide,
  category, or changelog entry disappears, the connector yields a dlt
  hard-delete marker; `merge` and Cognee orphan cleanup then remove it from
  memory. A suspicious empty sweep after a nonempty sync is protected and
  cannot erase the dataset.
- **Safe retry:** ReadMe rate-limit and transient server/network failures are
  retried. Other errors abort before the state cursor advances, so a later run
  retries the unchanged interval.

The API routes follow ReadMe's v2 branch/category/page and changelog endpoints;
the official API migration guide documents v2 Bearer authentication, branch
selection, and collection pagination.

## Test

```bash
uv run pytest tests/ -q
uv run ruff check .
uv run ruff format --check .
```

Tests use a fully mocked ReadMe API and cover first ingest, guide-body fetching,
the `updated_at` incremental cursor, category selection, one-version ID scoping,
hard-delete tombstones, and the empty-sweep safety guard. No ReadMe account or
network connection is needed.
