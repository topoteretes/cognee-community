"""Sync selected Miro boards into a dedicated cognee dataset."""

import asyncio
import os

import cognee

from cognee_community_connector_miro import miro_source

DATASET_NAME = "miro"


async def main() -> None:
    configured_board_ids = os.getenv("MIRO_BOARD_IDS") or os.getenv("MIRO_BOARD_ID", "")
    board_ids = [value.strip() for value in configured_board_ids.split(",") if value.strip()]
    if not os.getenv("MIRO_ACCESS_TOKEN") or not board_ids:
        print("Set MIRO_ACCESS_TOKEN and comma-separated MIRO_BOARD_IDS before running.")
        return

    await cognee.remember(
        miro_source(board_ids=board_ids),
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )

    result = await cognee.search(
        query_text="What decisions and action items are recorded in these Miro boards?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
