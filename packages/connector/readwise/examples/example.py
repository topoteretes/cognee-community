"""Sync a Readwise library into cognee and ask it questions.

Usage:
    export READWISE_API_KEY=...   # get one at https://readwise.io/access_token
    python examples/example.py
"""

import asyncio
import os

import cognee
from cognee_community_connector_readwise import readwise_source


async def main():
    if "READWISE_API_KEY" not in os.environ:
        raise SystemExit("Set READWISE_API_KEY first (https://readwise.io/access_token).")

    # Uncomment to scope what leaves Readwise:
    # source = readwise_source(book_ids=["123456"], category="books")
    source = readwise_source()

    # merge (not the default replace!) so incremental syncs upsert instead of
    # wiping the table.
    await cognee.remember(
        source,
        dataset_name="readwise",
        write_disposition="merge",
    )

    answer = await cognee.search(
        "What do my highlights say about deep work?",
        query_type="GRAPH_COMPLETION",
    )
    print("\n".join(answer))


if __name__ == "__main__":
    asyncio.run(main())
