"""Sync one public YouTube channel into cognee and search its videos."""

import asyncio

import cognee

from cognee_community_connector_youtube import youtube_source


async def main() -> None:
    await cognee.remember(
        youtube_source(),
        dataset_name="youtube",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        incremental_loading=False,
    )
    answer = await cognee.search(
        query_text="What topics are covered in these videos?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["youtube"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
