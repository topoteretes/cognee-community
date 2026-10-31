"""Storyblok connector demo: turn the stories of a space into memory.

``storyblok_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``. Every story (name, path, content type, tags, and the text of
its blocks and rich text fields) is ingested as a normal document.

Re-running syncs only what changed: while nothing was published, unpublished or
deleted the run costs one request, an edited story is sent again, and an
unpublished or deleted story is forgotten.

Privacy / opt-in
----------------
A preview token also reads drafts. Nothing is fetched until you run this script.
Keep Storyblok in its own dataset.

One-time setup
--------------
Copy a space access token (Settings, Access Tokens) and export it with the space's
region and your LLM key:

    export STORYBLOK_TOKEN="..."
    export STORYBLOK_REGION="eu"
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_storyblok import storyblok_source

DATASET_NAME = "storyblok"


async def main() -> None:
    if not os.environ.get("STORYBLOK_TOKEN"):
        print("Set STORYBLOK_TOKEN (and STORYBLOK_REGION if not eu) to run this example.")
        return

    # Narrow the sync with starts_with="blog/", content_type=..., or version="draft".
    source = storyblok_source(
        token=os.environ["STORYBLOK_TOKEN"],
        region=os.environ.get("STORYBLOK_REGION", "eu"),
    )

    print("Syncing Storyblok into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )
    print("Sync stats:", source.cognee_sync_stats)

    results = await cognee.recall(
        "What do our published pages say about pricing?", datasets=[DATASET_NAME]
    )
    print("\nRecall result:\n", results)


if __name__ == "__main__":
    asyncio.run(main())
