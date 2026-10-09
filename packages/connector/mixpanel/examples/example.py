"""Sync Mixpanel workspace knowledge into a cognee dataset."""

import asyncio

import cognee
from cognee_community_connector_mixpanel import mixpanel_source


async def main():
    await cognee.remember(
        mixpanel_source(include_events=True),
        dataset_name="mixpanel",
    )
    answer = await cognee.search("What does our activation cohort mean?", datasets=["mixpanel"])
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
