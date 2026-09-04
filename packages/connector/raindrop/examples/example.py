"""Raindrop.io connector demo — turn your bookmarks into memory.

Pull Raindrop.io collections, bookmarks, and highlights into cognee,
incrementally and with forget-on-delete. ``raindrop_source`` returns a ``dlt``
resource you hand straight to ``cognee.remember``. Rows are ingested as normal
documents (so they go through the full cognify entity-extraction pipeline).

The first run is a full backfill; every later run fetches only bookmarks
changed since the last sync (``lastUpdate`` cursor) and propagates deletions,
so bookmarks you delete upstream disappear from memory on the next sync.

Pass ``fetch_page_content=True`` to also fetch and index the linked pages'
text — opt-in, because it multiplies ingest cost.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your bookmarks (and, if opted in, the linked pages).
Nothing is fetched until you run this script. Use a dedicated dataset so you
can wipe it with a single ``cognee.forget``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[raindrop]"      # or: uv sync --extra raindrop

2. Create a test token at https://app.raindrop.io/#/settings/integrations
   (Settings → Integrations → For Developer).
3. Export the token and your LLM key, then run:

       export RAINDROP_API_TOKEN="..."
       export LLM_API_KEY="sk-..."
       uv run python packages/connector/raindrop/examples/example.py

Re-run after adding/deleting a bookmark to see the incremental re-sync and
forget-on-delete.
"""

import asyncio

import cognee

from cognee_community_connector_raindrop import raindrop_source


async def main():
    source = raindrop_source()  # reads RAINDROP_API_TOKEN
    # raindrop_source(fetch_page_content=True)  # also index linked pages

    print("=== Initial sync (full backfill) ===")
    result = await cognee.remember(
        source,
        dataset_name="raindrop_demo",
        primary_key="id",
        # "merge" is required: it's what makes re-runs incremental and what
        # makes deletions propagate via orphan cleanup. The default
        # ("replace") would wipe the synced corpus on every run.
        write_disposition="merge",
        # The DLT ingestion default caps a table at 50 rows; real accounts
        # exceed that, so lift the cap.
        max_rows_per_table=0,
    )
    print(result)

    answer = await cognee.recall("What do I have about machine learning?")
    print("Recall:", answer)

    print("\n=== Incremental re-sync (only changes/deletions are processed) ===")
    result = await cognee.remember(
        raindrop_source(),
        dataset_name="raindrop_demo",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
