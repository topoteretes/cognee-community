"""Run with TWITCH_ACCESS_TOKEN, TWITCH_CLIENT_ID and Cognee credentials set."""

import asyncio
import os
import sys

import cognee
from cognee_community_connector_twitch import twitch_source


async def main() -> None:
    source = twitch_source(
        access_token=os.environ["TWITCH_ACCESS_TOKEN"],
        client_id=os.environ["TWITCH_CLIENT_ID"],
        resource_name="my_twitch",
    )
    await cognee.remember(
        source,
        dataset_name="twitch",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    question = sys.argv[1] if len(sys.argv) > 1 else "Which channels cover Rust or Python?"
    print(await cognee.recall(question, datasets=["twitch"]))


if __name__ == "__main__":
    asyncio.run(main())
