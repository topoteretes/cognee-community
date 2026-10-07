"""Sync a single ReadMe documentation branch into a dedicated Cognee dataset."""

from __future__ import annotations

import asyncio
import os

import cognee

from cognee_community_connector_readme import readme_source

DATASET_NAME = "readme_docs"


async def main() -> None:
    if not os.environ.get("README_API_KEY"):
        print("Set README_API_KEY (a ReadMe v2 API key) before running this example.")
        return

    # Pick one branch only. ``stable`` is production; replace it with a named
    # preview branch to index that version instead of creating cross-version
    # duplicate memories. Limit categories if the project is large.
    source = readme_source(branch="stable", category_titles=None, include_changelog=True)
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="What changed recently in this API?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
