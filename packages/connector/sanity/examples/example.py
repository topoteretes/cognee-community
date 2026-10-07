"""Example: ingest Sanity CMS documents into cognee.

Run:
    SANITY_PROJECT_ID=your-project-id SANITY_API_TOKEN=your-token python examples/example.py
"""

import asyncio
import os

import cognee
from cognee_community_connector_sanity import sanity_source


async def main() -> None:
    cognee.config.set_data_root_directory(".cognee-data")
    cognee.config.set_db_path(".cognee-data")
    await cognee.infrastructure.engine.connect()

    source = sanity_source(
        project_id=os.environ.get("SANITY_PROJECT_ID"),
        api_token=os.environ.get("SANITY_API_TOKEN"),
        dataset="production",
        document_types=["post", "page"],
        groq_filter="status == 'published'",
    )

    print("Ingesting Sanity documents …")
    await cognee.add(source)
    print("Done. Try cognee.search(\"summarize my latest blog posts\")")


if __name__ == "__main__":
    asyncio.run(main())
