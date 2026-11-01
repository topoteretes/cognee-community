"""Example: ingest Todoist tasks into cognee.

Run:
    TODOIST_API_TOKEN=your-token python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_todoist import todoist_source


async def main() -> None:
    cognee.config.set_data_root_directory(".cognee-data")
    cognee.config.set_db_path(".cognee-data")
    await cognee.infrastructure.engine.connect()

    source = todoist_source(
        api_token=os.environ.get("TODOIST_API_TOKEN"),
    )

    print("Ingesting Todoist tasks …")
    await cognee.add(source)
    print('Done. Try cognee.search("what high-priority tasks are due this week?")')


if __name__ == "__main__":
    asyncio.run(main())
