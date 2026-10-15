"""Example: ingest Otter.ai meeting transcripts into cognee.

Run:
    OTTER_API_KEY=your-key python examples/example.py
"""

import asyncio
import os

import cognee
from cognee_community_connector_otter import otter_source


async def main() -> None:
    cognee.config.set_data_root_directory(".cognee-data")
    cognee.config.set_db_path(".cognee-data")
    await cognee.infrastructure.engine.connect()

    source = otter_source(
        api_key=os.environ.get("OTTER_API_KEY"),
        include_shared=False,
    )

    print("Ingesting Otter.ai conversations …")
    await cognee.add(source)
    print('Done. Try cognee.search("summarize my recent product meetings")')


if __name__ == "__main__":
    asyncio.run(main())
