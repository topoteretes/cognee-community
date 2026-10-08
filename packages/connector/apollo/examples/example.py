"""Sync an Apollo.io workspace into cognee memory, incrementally.

Set APOLLO_API_KEY (Apollo: Settings > Integrations > API) and the usual cognee
LLM_API_KEY, then run `python examples/example.py`. Running it again only processes
records that changed in Apollo and forgets the ones deleted there.
"""

import asyncio
import os

import cognee

from cognee_community_connector_apollo import apollo_source

DATASET = "apollo_demo"


async def sync() -> None:
    source = apollo_source(api_key=os.environ["APOLLO_API_KEY"])
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        # required: "merge" keeps re-runs incremental and lets deletions propagate
        write_disposition="merge",
    )
    print("sync stats:", source.cognee_sync_stats)


async def main() -> None:
    await sync()

    answer = await cognee.recall(
        "Which contacts are enrolled in a sequence, and at which companies?",
        datasets=[DATASET],
    )
    print("recall:", answer)

    # a second run only processes what changed since the first one
    await sync()


if __name__ == "__main__":
    asyncio.run(main())
