"""Example showing how to sync Canvas LMS data into cognee memory."""

import asyncio
import os

import cognee

from cognee_community_connector_canvas import canvas_source


async def main() -> None:
    token = os.environ.get("CANVAS_ACCESS_TOKEN", "mock_canvas_token")
    base_url = os.environ.get("CANVAS_BASE_URL", "https://canvas.instructure.com")

    # Sync Canvas LMS course contents into cognee graph memory
    source = canvas_source(
        access_token=token,
        base_url=base_url,
        resources=["pages", "modules", "assignments", "announcements"],
    )

    await cognee.remember(
        source,
        dataset_name="canvas_lms",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    # Query the memory layer
    results = await cognee.search(
        "What are the upcoming course assignments and module materials?",
        dataset_name="canvas_lms",
    )
    for r in results:
        print(f"Memory Search Result: {r}")


if __name__ == "__main__":
    asyncio.run(main())
