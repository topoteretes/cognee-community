"""Example usage of the Cognee Community Mastra.ai Connector."""

import asyncio
import os
import cognee
from cognee_community_connector_mastra import mastra_source


async def main():
    # 1. Initialize Mastra connector
    source = mastra_source(
        api_url=os.environ.get("MASTRA_API_URL", "http://localhost:4111"),
        api_key=os.environ.get("MASTRA_API_KEY", "optional_api_key"),
        include_tools=True,
    )

    # 2. Add source to Cognee
    print("Ingesting Mastra conversation memory into Cognee...")
    await cognee.add(source)

    # 3. Cognify memory (extract entities, relationships, and vector embeddings)
    print("Running Cognify entity extraction...")
    await cognee.cognify()

    # 4. Search cognitive memory
    query = "What decisions were made during recent agent conversations?"
    print(f"Searching memory for: '{query}'")
    results = await cognee.search(query)
    print("Results:")
    for res in results:
        print(f" - {res}")


if __name__ == "__main__":
    asyncio.run(main())
