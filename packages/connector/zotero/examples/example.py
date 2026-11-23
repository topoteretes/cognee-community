"""Zotero connector demo."""

import asyncio
import os

import cognee
from cognee_community_connector_zotero import zotero_source

DATASET_NAME = "zotero"


async def main() -> None:
    if not os.environ.get("ZOTERO_API_KEY") or not os.environ.get("ZOTERO_LIBRARY_ID"):
        print("Set ZOTERO_API_KEY and ZOTERO_LIBRARY_ID to run this example.")
        return

    source = zotero_source()
    print("Syncing Zotero library into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Summarize the main themes in my library.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\\nSearch result:\\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
