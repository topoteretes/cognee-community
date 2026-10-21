"""Raindrop.io connector demo — turn your bookmarks and highlights into memory.

``raindrop_source`` returns a ``dlt`` resource you hand straight to
``cognee.remember``. Each bookmark becomes one document (title, link,
collection path, tags, description, note, and highlights), so it goes through
the full cognify entity-extraction pipeline.

Re-running is incremental: only new or changed bookmarks are re-ingested, and
bookmarks you delete or move to Trash are forgotten on the next run.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads your bookmarks, notes, and highlights. Nothing is fetched until you
run this script. Scope it with ``collection_ids=[...]``, and keep it in its own
dataset so you can wipe it with a single ``cognee.prune``. Fetching the linked
pages themselves is a separate opt-in (``fetch_page_content=True``).

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector:

       uv pip install cognee-community-connector-raindrop

2. Create an app at https://app.raindrop.io/settings/integrations and copy its
   "Test token" (an OAuth access token works too).
3. Export the token and your LLM key, then run:

       export RAINDROP_TOKEN="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Edit a bookmark, add a highlight, or trash one, then re-run to see the
incremental sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_raindrop import raindrop_source

# Keep Raindrop in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "raindrop"


async def main() -> None:
    if not os.environ.get("RAINDROP_TOKEN"):
        print("Set RAINDROP_TOKEN (a Raindrop.io test token) to run this example.")
        return

    # Scope with collection_ids=[...] (-1 is Unsorted); omit it to sync every
    # bookmark outside Trash. Add fetch_page_content=True to also ingest the
    # linked pages (slower and more tokens).
    source = raindrop_source()

    print("Syncing Raindrop.io bookmarks into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        # Required: each run only emits new/changed bookmarks plus deletions, so
        # staging must be merged, not replaced (cognee's default).
        write_disposition="merge",
    )

    answer = await cognee.search(
        query_text="What topics do my bookmarks and highlights cover?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit a bookmark or trash one in Raindrop.io, then re-run: changes "
        "re-sync and removed bookmarks are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
