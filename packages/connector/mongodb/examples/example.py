"""MongoDB connector demo — "ask my database".

Pulls documents out of a MongoDB collection into cognee memory, incrementally and
with forget-on-delete. The first run backfills the collection; re-running syncs only
the documents modified since the cursor, and documents deleted in MongoDB are
forgotten from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector:

       uv pip install cognee-community-connector-mongodb
       # or, from this monorepo: cd packages/connector/mongodb && uv sync

2. Have a MongoDB instance running. A throwaway one is enough:

       docker run -d -p 27017:27017 --name cognee-mongo mongo:8

3. Seed something to ask about:

       docker exec cognee-mongo mongosh --quiet --eval '
         db = db.getSiblingDB("support");
         db.tickets.insertMany([
           { subject: "SSO login fails", body: "Okta redirect loop after upgrade",
             status: "open", updatedAt: ISODate("2026-01-01") },
           { subject: "Invoice PDF wrong currency", body: "Totals show EUR not USD",
             status: "open", updatedAt: ISODate("2026-01-02") }
         ]);'

4. Export your connection details (access is read-only — the connector only issues
   find()):

       export MONGODB_URI="mongodb://localhost:27017"
       export MONGODB_DATABASE="support"
       export MONGODB_COLLECTION="tickets"

5. Index the cursor field so incremental syncs stay cheap:

       docker exec cognee-mongo mongosh --quiet --eval \
         'db.getSiblingDB("support").tickets.createIndex({ updatedAt: 1 })'

6. Set your LLM key (LLM_API_KEY) in .env like any other cognee example.

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_mongodb import mongodb_source

# Keep the collection in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "mongodb_tickets"

# Routing kwargs shared by every remember() call below.
#
# write_disposition="merge" is REQUIRED. The add pipeline defaults to "replace",
# which would rewrite staging on every run and drop the corpus the incremental
# cursor is diffing against.
#
# primary_key="id" is the stringified document _id, so a re-synced document
# upserts instead of duplicating.
#
# max_rows_per_table=0 is not needed here: for document-mode sources cognee
# already reads back the whole synced corpus so orphan cleanup compares against
# all of it. It is passed explicitly only to mirror the other connectors.
MONGODB_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
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
            # churning the document text (and its content-hash data_id) downstream.
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
    # MongoDB is removed from memory by orphan cleanup. Change the collection
    # between the two runs to see the delta in action, e.g.:
    #
    #   db.tickets.deleteOne({ subject: "Invoice PDF wrong currency" })
    #   db.tickets.updateOne({ subject: "SSO login fails" },
    #                        { $set: { body: "Resolved after Okta cert renewal",
    #                                  updatedAt: ISODate("2026-02-01") } })
    print("\n=== MongoDB sync #2 (incremental) ===")
    result = await cognee.remember(
        build_source(), dataset_name=DATASET_NAME, **MONGODB_REMEMBER_KWARGS
    )
    print(result)

    answer = await cognee.search(
        query_text="Which tickets mention SSO or Okta login failures?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("SSO answer:", answer)


if __name__ == "__main__":
    asyncio.run(main())
