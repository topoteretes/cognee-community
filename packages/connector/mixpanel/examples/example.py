"""Run selected ingestion and retrieve definitions with the Cognee public API."""

import asyncio
import os
from pathlib import Path

import cognee
from dotenv import load_dotenv

from cognee_community_connector_mixpanel import mixpanel_source, sync_mixpanel


async def main():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    workspace = os.getenv("MIXPANEL_WORKSPACE_ID")
    source = mixpanel_source(workspace_id=int(workspace) if workspace else None)
    dataset = await sync_mixpanel(source)
    results = await cognee.search(
        query_text="How are activation cohorts defined?",
        query_type=cognee.SearchType.CHUNKS,
        datasets=[dataset],
    )
    for result in results:
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
