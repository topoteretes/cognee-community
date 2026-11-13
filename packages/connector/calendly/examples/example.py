"""Calendly connector demo — sync your scheduled meetings into cognee memory.

Pulls Calendly events and invitee question responses into cognee with forget-on-delete.
``calendly_source`` returns a dlt source for ``cognee.remember``. Events are ingested as
normal documents and processed through the full cognify knowledge graph extraction.

Requirements:
- CALENDLY_API_KEY (Personal Access Token from Calendly)
- LLM_API_KEY (for Cognee cognify entity extraction)
"""

import asyncio
import os

import cognee

from cognee_community_connector_calendly import calendly_source

DATASET_NAME = "calendly"


async def main() -> None:
    token = os.environ.get("CALENDLY_API_KEY") or os.environ.get("CALENDLY_ACCESS_TOKEN")
    if not token:
        print("Please set CALENDLY_API_KEY (or CALENDLY_ACCESS_TOKEN) to run this example.")
        return

    print("Connecting to Calendly and ingesting scheduled meetings...")
    source = calendly_source()

    # Ingest meetings into the dedicated dataset
    await cognee.remember(source, dataset_name=DATASET_NAME)

    print("\nRunning cognitive search over ingested meetings...")
    answer = await cognee.search(
        query_text="Summarize the scheduled meetings and any questions asked by invitees.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch Result:")
    print(answer)

    print(
        "\nNote: Any canceled or removed meetings upstream will be pruned from memory "
        "on subsequent syncs via cognee's forget-on-delete orphan cleanup."
    )


if __name__ == "__main__":
    asyncio.run(main())
