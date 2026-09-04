"""Contentful connector demo — turn your CMS into memory.

Pull Contentful content types (the content model), entries, and assets into
cognee, incrementally and with forget-on-delete. ``contentful_source`` returns
a ``dlt`` resource you hand straight to ``cognee.remember``. Rows are ingested
as normal documents (so they go through the full cognify entity-extraction
pipeline).

The first run is a full backfill; every later run fetches only objects changed
since the last sync (``sys.updatedAt`` cursor) and propagates deletions, so
entries you delete in Contentful disappear from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your Contentful space. Nothing is fetched until you
run this script. Use a dedicated dataset so you can wipe it with a single
``cognee.forget``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[contentful]"      # or: uv sync --extra contentful

2. In Contentful: Settings → API keys → Add API key; copy the *Content
   Delivery API - access token* and the Space ID.
3. Export them and your LLM key, then run:

       export CONTENTFUL_SPACE_ID="..."
       export CONTENTFUL_TOKEN="..."
       export LLM_API_KEY="sk-..."
       uv run python packages/connector/contentful/examples/example.py

Re-run after editing/deleting an entry to see the incremental re-sync and
forget-on-delete.
"""

import asyncio

import cognee

from cognee_community_connector_contentful import contentful_source


async def main():
    source = contentful_source()  # reads CONTENTFUL_SPACE_ID / CONTENTFUL_TOKEN

    print("=== Initial sync (full backfill) ===")
    result = await cognee.remember(
        source,
        dataset_name="contentful_demo",
        primary_key="id",
        # "merge" is required: it's what makes re-runs incremental and what
        # makes deletions propagate via orphan cleanup. The default
        # ("replace") would wipe the synced corpus on every run.
        write_disposition="merge",
        # The DLT ingestion default caps a table at 50 rows; real spaces
        # exceed that, so lift the cap.
        max_rows_per_table=0,
    )
    print(result)

    answer = await cognee.recall("What do we publish about pricing?")
    print("Recall:", answer)

    print("\n=== Incremental re-sync (only changes/deletions are processed) ===")
    result = await cognee.remember(
        contentful_source(),
        dataset_name="contentful_demo",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
