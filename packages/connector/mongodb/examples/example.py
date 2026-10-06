"""MongoDB connector demo — "ask my database".

Pull documents out of a MongoDB collection into cognee memory, incrementally,
with forget-on-delete.

This example is built on cognee's DLT ingestion subsystem: ``mongodb_source``
returns a ``dlt`` resource that you hand straight to ``cognee.remember``. The
first run backfills the collection; re-running ``remember`` syncs only documents
modified since (via the ``updatedAt`` cursor), and documents you delete in
MongoDB are forgotten from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector:

       uv pip install cognee-community-connector-mongodb
       # or, from this monorepo: cd packages/connector/mongodb && uv sync

2. Have a MongoDB instance running. A throwaway one is enough:

       docker run -d -p 27017:27017 --name cognee-mongo mongo:7

3. Export your connection details (access is read-only — the connector only
   issues find()):

       export MONGODB_URI="mongodb://localhost:27017"
       export MONGODB_DATABASE="support"
       export MONGODB_COLLECTION="tickets"

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

5. Index the cursor field so incremental syncs stay cheap:

       db.tickets.createIndex({ updatedAt: 1 })

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_mongodb import mongodb_source

# Keep the collection in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "mongodb_tickets"

# Routing kwargs shared by every remember() call below. ``max_rows_per_table=0``
# disables cognee's per-table read cap so orphan-cleanup (forget-on-delete)
# compares against the *entire* synced corpus, not a 50-row window.
MONGODB_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def main():
    uri = os.environ.get("MONGODB_URI")
    database = os.environ.get("MONGODB_DATABASE")
    collection = os.environ.get("MONGODB_COLLECTION")

    if not all([uri, database, collection]):
        print(
            "Set MONGODB_URI, MONGODB_DATABASE and MONGODB_COLLECTION.\n"
            "See the setup steps in this file's docstring, then re-run."
        )
        return

    # Start from a clean slate so the demo is reproducible.
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    def build_source():
        return mongodb_source(
            uri=uri,
            database=database,
            collection=collection,
            # MongoDB is schemaless, so name the fields explicitly. Anything not
            # named here is dropped, which keeps a metadata-only write from
            # churning the document text downstream.
            text_fields=["subject", "body"],
            title_field="subject",
        )

    # ── First sync: full backfill ──────────────────────────────────────────
    print("\n=== MongoDB sync #1 (backfill) ===")
    result = await cognee.remember(
        build_source(), dataset_name=DATASET_NAME, **MONGODB_REMEMBER_KWARGS
    )
    print(result)

    answer = await cognee.search(
        query_text="Summarize the most common themes across these tickets.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Ticket summary:", answer)

    # ── Second sync: incremental delta + forget-on-delete ──────────────────
    # Re-running with the SAME dataset reuses the persisted cursor: only
    # documents modified since sync #1 are fetched, and anything deleted in
    # MongoDB is removed from memory by orphan_cleanup.
    print("\n=== MongoDB sync #2 (incremental) ===")
    result = await cognee.remember(
        build_source(), dataset_name=DATASET_NAME, **MONGODB_REMEMBER_KWARGS
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
