"""Zotero connector demo - turn your Zotero library into memory.

Version-based incremental sync + forget-on-delete. Each run yields only
items changed since the last sync, and cancelled/removed items drop out
of the snapshot so cognee's orphan_cleanup forgets them.

    export ZOTERO_API_KEY="..."
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_zotero import zotero_source

DATASET_NAME = "zotero"


async def main() -> None:
    if not os.environ.get("ZOTERO_API_KEY"):
        print("Set ZOTERO_API_KEY to run this example.")
        return

    source = zotero_source()

    print("Syncing Zotero library into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
    )

    answer = await cognee.search(
        query_text="Summarize what these references are about.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nAdd or delete items in Zotero, then re-run: "
        "changes sync incrementally and removed items are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
