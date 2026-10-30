"""Readwise connector demo — turn your highlights into memory.

Pull your Readwise highlights and notes into cognee, incrementally and with
forget-on-delete. ``readwise_source`` returns a ``dlt`` resource you hand to
``cognee.remember``. Each highlight becomes a normal document, so it goes through
the full cognify entity-extraction pipeline.

First run: backfills everything. Later runs: only highlights changed since the
last run are fetched (``updatedAfter``). Highlights or books you delete in
Readwise are forgotten from memory on the next run.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads your highlights and notes. Nothing is fetched until you run this
script. Use ``categories=[...]`` / ``book_ids=[...]`` to limit what is ingested,
and a dedicated dataset so you can wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector:

       cd packages/connector/readwise && uv sync --all-extras

2. Copy your access token from https://readwise.io/access_token.
3. Export the token and your LLM key, then run:

       export READWISE_TOKEN="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Run it again after adding/deleting a highlight to see the incremental re-sync.
"""

import asyncio
import os

import cognee

from cognee_community_connector_readwise import readwise_source

# Keep Readwise in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "readwise"


async def main() -> None:
    if not os.environ.get("READWISE_TOKEN"):
        print("Set READWISE_TOKEN (https://readwise.io/access_token) to run this example.")
        return

    # Limit with categories=["books", "articles"] or book_ids=[...]; omit for all.
    source = readwise_source()

    print("Syncing Readwise highlights into cognee ...")
    # write_disposition="merge" is REQUIRED: incremental runs only see changes.
    await cognee.remember(source, dataset_name=DATASET_NAME, write_disposition="merge")

    answer = await cognee.search(
        query_text="What are the main ideas across my highlights?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nHighlight something new (or delete one) in Readwise, then re-run: only the "
        "changes are fetched and deletions are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
