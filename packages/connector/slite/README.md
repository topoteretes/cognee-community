# cognee-community-connector-slite

A Slite data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your team's runbooks, policies, and internal documentation into memory — "ask my team's documentation".

It exposes a `dlt` resource you hand to `cognee.remember(...)`, integrating with Cognee's DLT ingestion path:
- **Preserved review states:** Slite review flags (`Verified`, `Outdated`, `VerificationRequested`, `VerificationExpired`) are recorded as document metadata and searchable node sets (`slite:review:<state>`).
- **Scoped subtree ingestion:** Choose specific parent notes to sync entire subtrees (`includeDescendants=true`), or ingest the entire workspace.
- **Incremental change detection:** Only notes whose metadata or timestamp changed have their markdown bodies re-fetched.
- **Safe forget-on-delete:** Archived and hard-deleted notes in Slite are automatically purged with `_deleted: True` tombstones on the next sync, guarded by a `/me` account verification check.

Resolves [topoteretes/cognee#6153](https://github.com/topoteretes/cognee/issues/6153).

## Requirements

- Python 3.10 to 3.14.
- `cognee>=1.6.3,<2`
- `dlt[sqlalchemy]>=1.9.0,<2`
- `requests>=2.31,<3`
- A Slite API key from **Settings → API**.

## Install

```bash
uv pip install cognee-community-connector-slite
# or from this monorepo:
cd packages/connector/slite && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_slite import slite_source

# 1. Sync Slite documentation into Cognee memory
await cognee.remember(
    slite_source(
        api_key="...",
        resource_name="slite_company_knowledge",
        parent_note_ids=["parent_note_123"],  # optional: scope to specific subtrees
    ),
    dataset_name="company_docs",
    primary_key="id",
    write_disposition="merge",  # required for incremental upsert
    max_rows_per_table=0,
)

# 2. Search knowledge base with review state awareness
answer = await cognee.search(
    query_text="What is our customer refund policy, and is it verified?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["company_docs"],
)
```

## Review States & Node Sets

Slite marks documents with lifecycle statuses. The connector maps these into search node sets:
- `slite:review:Verified`: Verified documentation.
- `slite:review:Outdated`: Deprecated or out-of-date documentation.
- `slite:review:VerificationRequested`: Docs awaiting author/reviewer verification.
- `slite:review:VerificationExpired`: Docs whose verification timer has elapsed.

Channels are automatically tagged with `slite:channel:<title>`, allowing queries to filter by workspace channels.

## Incremental Sync & Deletion Safety

1. **Incremental Sync:** Each sync queries note metadata. If `lastEditedAt`, `updatedAt`, or `reviewState` have not changed, the connector skips fetching the markdown body, saving bandwidth and respecting API rate limits.
2. **Payload Protection:** Notes exceeding API size limits (HTTP 413) are recorded with a summary placeholder rather than failing the sync pipeline.
3. **Safe Forget-on-Delete:** Before any `_deleted: True` tombstone is written, the connector checks `GET /me`. If the API key belongs to a different user or team than the previous run, deletions are aborted immediately to prevent unintended data loss.

## Running Tests

```bash
PYTHONPATH=packages/connector/slite:packages/connector/slite/tests pytest packages/connector/slite/tests/test_slite.py -v
```
