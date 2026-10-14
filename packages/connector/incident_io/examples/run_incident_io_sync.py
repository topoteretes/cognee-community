"""Example demonstrating how to sync incident.io incidents into Cognee memory."""

import asyncio
import os
import cognee
from cognee_community_connector_incident_io import incident_io_source


async def main():
    api_key = os.environ.get("INCIDENT_IO_API_KEY", "mock_key")
    print(f"Connecting to incident.io and synchronizing incidents...")

    # Build the incident.io dlt source
    source = incident_io_source(api_key=api_key)

    # Ingest incidents into Cognee
    print("Ingesting incidents into Cognee memory...")
    await cognee.add(source)

    # Cognify extracts entities and constructs the operational knowledge graph
    print("Cognifying operational memory graph...")
    await cognee.cognify()

    # Search past incident memory
    query = "What caused our database connection pool exhaustion?"
    print(f"\nSearching Cognee memory: '{query}'")
    results = await cognee.search(query)
    print("Results:")
    for res in results:
        print(f"- {res}")


if __name__ == "__main__":
    asyncio.run(main())
