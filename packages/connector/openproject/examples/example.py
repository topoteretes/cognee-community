"""Example: ingest OpenProject work packages into cognee.

Run:
    OPENPROJECT_BASE_URL=https://openproject.example.com \
    OPENPROJECT_API_KEY=your-key \
    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_openproject import openproject_source


async def main() -> None:
    cognee.config.set_data_root_directory(".cognee-data")
    cognee.config.set_db_path(".cognee-data")
    await cognee.infrastructure.engine.connect()

    source = openproject_source(
        base_url=os.environ.get("OPENPROJECT_BASE_URL"),
        api_key=os.environ.get("OPENPROJECT_API_KEY"),
    )

    print("Ingesting OpenProject work packages …")
    await cognee.add(source)
    print('Done. Try cognee.search("what tasks are in progress for the Q3 release?")')


if __name__ == "__main__":
    asyncio.run(main())
