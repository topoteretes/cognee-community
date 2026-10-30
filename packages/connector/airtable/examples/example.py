"""Ingest an Airtable base into cognee memory, incrementally.

Setup (once):

1. Create a personal access token with the ``data.records:read`` scope (add
   ``schema.bases:read`` too if you want the field schema ingested).
2. Export it as ``AIRTABLE_API_KEY`` along with ``AIRTABLE_BASE_ID`` you can read.
3. Add a **`lastModifiedTime`** field of type *Last modified time* to every table you
   sync — Airtable does not expose a modified time on the record itself, so this
   field is what makes the incremental cursor work. See the connector README.
4. Set ``LLM_API_KEY`` like any other cognee run.

Then:

    uv run python examples/example.py

The second ``remember`` call in this script is the interesting one: it reuses the
persisted cursor, so only records changed since the first run are re-ingested and
anything deleted upstream is forgotten.
"""

import asyncio
import os

import cognee

from cognee_community_connector_airtable import airtable_source

# Keep the base in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "airtable_base"

# Routing kwargs shared by every remember() call below. ``max_rows_per_table=0``
# disables cognee's per-table read cap so orphan-cleanup (forget-on-delete)
# compares against the *entire* synced corpus, not a 50-row window.
# ``write_disposition="merge"`` is REQUIRED: without it the add pipeline defaults
# to "replace" and drops the table on every run instead of upserting by id.
AIRTABLE_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def main():
    base_id = os.environ.get("AIRTABLE_BASE_ID")
    token = os.environ.get("AIRTABLE_API_KEY")
    table_ids = os.environ.get("AIRTABLE_TABLE_IDS")

    if not all([base_id, token]):
        print(
            "Set AIRTABLE_BASE_ID and AIRTABLE_API_KEY.\n"
            "See the setup steps in this file's docstring, then re-run."
        )
        return

    # Start from a clean slate so the demo is reproducible.
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    def build_source():
        return airtable_source(
            base_id=base_id,
            table_ids=table_ids.split(",") if table_ids else None,
            token=token,
        )

    # ── First sync: full backfill ──────────────────────────────────────────
    print("\n=== Airtable sync #1 (backfill) ===")
    result = await cognee.remember(
        build_source(), dataset_name=DATASET_NAME, **AIRTABLE_REMEMBER_KWARGS
    )
    print(result)

    answer = await cognee.search(
        query_text="Summarize the records in this base.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Base summary:", answer)

    # ── Second sync: incremental delta + forget-on-delete ──────────────────
    # Re-running with the SAME dataset reuses the persisted cursor: only records
    # whose lastModifiedTime is newer than sync #1 are re-ingested, and anything
    # deleted in Airtable is removed from memory by orphan_cleanup.
    print("\n=== Airtable sync #2 (incremental) ===")
    result = await cognee.remember(
        build_source(), dataset_name=DATASET_NAME, **AIRTABLE_REMEMBER_KWARGS
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
