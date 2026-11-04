"""Slite connector demo — "ask my team's runbooks and policies".

Pull your Slite notes, review states, and runbooks into Cognee memory,
incrementally, with forget-on-delete.

This example is built on Cognee's DLT ingestion subsystem: `slite_source`
returns a resource that you hand straight to `cognee.remember`. The first
run ingests the workspace or chosen parent notes; re-running `remember`
re-processes only notes whose content changed, and notes you archive or
delete in Slite are forgotten from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:
       pip install cognee-community-connector-slite

2. Create an API key in Slite under Settings -> API.

3. Export it:
       export SLITE_API_KEY="…"

4. Run it:
       python examples/example.py
"""

import asyncio
import os

from cognee_community_connector_slite import slite_source

DATASET_NAME = "slite_docs"
RESOURCE_NAME = "slite_company_knowledge"

SLITE_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def sync(api_key: str, parent_note_ids: list[str] | None = None) -> None:
    try:
        import cognee
    except ImportError:
        print("cognee is not installed. Testing pure source iteration:")
        source = slite_source(
            api_key=api_key,
            resource_name=RESOURCE_NAME,
            parent_note_ids=parent_note_ids,
        )
        for doc in source:
            print(f"- Synced note: {doc.get('title')} (review: {doc.get('review_state')})")
        print("Sync stats:", getattr(source, "cognee_sync_stats", {}))
        return

    source = slite_source(
        api_key=api_key,
        resource_name=RESOURCE_NAME,
        parent_note_ids=parent_note_ids,
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **SLITE_REMEMBER_KWARGS)
    print("Ingestion result:", result)
    print("Sync stats:", source.cognee_sync_stats)


async def main():
    api_key = os.getenv("SLITE_API_KEY")
    if not api_key:
        print("Please export SLITE_API_KEY to run the live example.")
        return

    print("--- 1. First sync: ingest notes into memory ---")
    await sync(api_key)

    print("\n--- 2. Second sync: incremental change detection ---")
    await sync(api_key)


if __name__ == "__main__":
    asyncio.run(main())
