"""Sync selected Todoist records into cognee and search them."""

import asyncio

import cognee

from cognee_community_connector_todoist import todoist_source


async def main() -> None:
    await cognee.remember(
        todoist_source(
            include_projects=True,
            include_tasks=True,
            include_comments=True,
        ),
        dataset_name="todoist",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        incremental_loading=False,
    )

    answer = await cognee.search(
        query_text="What tasks and projects do I have in Todoist?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["todoist"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
