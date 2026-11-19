"""Example: ingest BookStack wiki pages into cognee.

Run:
    BOOKSTACK_BASE_URL=https://your-wiki.example.com \
    BOOKSTACK_TOKEN_ID=your-token-id \
    BOOKSTACK_TOKEN_SECRET=your-token-secret \
    python examples/example.py
"""

import asyncio
import os

import cognee
from cognee_community_connector_bookstack import bookstack_source


async def main() -> None:
    cognee.config.set_data_root_directory(".cognee-data")
    cognee.config.set_db_path(".cognee-data")
    await cognee.infrastructure.engine.connect()

    source = bookstack_source(
        base_url=os.environ.get("BOOKSTACK_BASE_URL"),
        token_id=os.environ.get("BOOKSTACK_TOKEN_ID"),
        token_secret=os.environ.get("BOOKSTACK_TOKEN_SECRET"),
    )

    print("Ingesting BookStack pages …")
    await cognee.add(source)
    print('Done. Try cognee.search("how do I configure the deployment pipeline?")')


if __name__ == "__main__":
    asyncio.run(main())
