"""Example: ingest Discourse forum topics into cognee.

Run:
    DISCOURSE_BASE_URL=https://forum.example.com python examples/example.py
"""

import asyncio
import os

import cognee
from cognee_community_connector_discourse import discourse_source


async def main() -> None:
    cognee.config.set_data_root_directory(".cognee-data")
    cognee.config.set_db_path(".cognee-data")
    await cognee.infrastructure.engine.connect()

    source = discourse_source(
        base_url=os.environ.get("DISCOURSE_BASE_URL"),
        api_key=os.environ.get("DISCOURSE_API_KEY"),
        api_username=os.environ.get("DISCOURSE_API_USERNAME"),
    )

    print("Ingesting Discourse topics …")
    await cognee.add(source)
    print('Done. Try cognee.search("how do I configure the plugin?")')


if __name__ == "__main__":
    asyncio.run(main())
