"""Example demonstrating how to ingest OpenStreetMap places into Cognee."""

import asyncio
import cognee
from cognee_community_connector_openstreetmap import openstreetmap_source


async def main() -> None:
    # 1. Define bounding box for central London (or any city/neighborhood)
    # format: (min_latitude, min_longitude, max_latitude, max_longitude)
    central_london_bbox = (51.500, -0.130, 51.515, -0.110)

    # 2. Configure OpenStreetMap source to fetch cafes, libraries, and parks
    source = openstreetmap_source(
        bbox=central_london_bbox,
        amenities=["cafe", "library", "park"],
        limit=25,
    )

    # 3. Ingest places into Cognee memory
    print("Ingesting OpenStreetMap places into Cognee...")
    await cognee.add(source)

    # 4. Cognify into knowledge graph
    print("Running Cognify extraction pipeline...")
    await cognee.cognify()

    # 5. Ask natural language questions about local places
    query = "Which cafes or libraries are tagged as wheelchair accessible and have opening hours listed?"
    print(f"\nSearching Cognee memory: '{query}'")
    results = await cognee.search(query)
    for result in results:
        print(f"- {result}")


if __name__ == "__main__":
    asyncio.run(main())
