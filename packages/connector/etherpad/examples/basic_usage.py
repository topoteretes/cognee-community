"""Example usage of the Cognee Community Etherpad Connector."""

import asyncio
import os
import cognee
from cognee_community_connector_etherpad import etherpad_source


async def main():
    # 1. Initialize Etherpad connector
    source = etherpad_source(
        base_url=os.environ.get("ETHERPAD_BASE_URL", "http://localhost:9001"),
        api_key=os.environ.get("ETHERPAD_API_KEY", "your_etherpad_api_key"),
        pad_prefix="meeting-",
        include_chat=True,
    )

    # 2. Add source to Cognee
    print("Ingesting Etherpad documents into Cognee...")
    await cognee.add(source)

    # 3. Cognify memory (extract entities, relationships, and vector embeddings)
    print("Running Cognify entity extraction...")
    await cognee.cognify()

    # 4. Search cognitive memory
    query = "What decisions were made regarding infrastructure scaling?"
    print(f"Searching memory for: '{query}'")
    results = await cognee.search(query)
    print("Results:")
    for res in results:
        print(f" - {res}")


if __name__ == "__main__":
    asyncio.run(main())
