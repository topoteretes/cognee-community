"""Runnable example: sync Todoist into Cognee and search."""

import asyncio
import os

import cognee
from cognee_community_connector_todoist import todoist_source

DATASET_NAME = "todoist"


async def main() -> None:
    if not os.environ.get("TODOIST_API_KEY"):
        print("Set TODOIST_API_KEY (get it from https://todoist.com/prefs/integrations)")
        return

    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY for Cognee's entity extraction (or use local models)")
        return

    source = todoist_source()

    print("Syncing Todoist data into Cognee...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What are my current active tasks?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nComplete or delete a task in Todoist, then re-run: "
        "completed tasks persist, deleted tasks are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())