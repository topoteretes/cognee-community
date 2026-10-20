"""Example usage of the Cognee Community Zoho Cliq Connector."""

import asyncio
import os
import cognee
from cognee_community_connector_zoho_cliq import zoho_cliq_source


async def main():
    # 1. Initialize Zoho Cliq connector
    source = zoho_cliq_source(
        auth_token=os.environ.get("ZOHO_CLIQ_AUTH_TOKEN", "your_zoho_cliq_token"),
        domain=os.environ.get("ZOHO_CLIQ_DOMAIN", "cliq.zoho.com"),
        channel_names=["general", "engineering-sync"],
        include_threads=True,
    )

    # 2. Add source to Cognee
    print("Ingesting Zoho Cliq chat history into Cognee...")
    await cognee.add(source)

    # 3. Cognify memory (extract entities, relationships, and vector embeddings)
    print("Running Cognify entity extraction...")
    await cognee.cognify()

    # 4. Search cognitive memory
    query = "What decisions were discussed in #engineering-sync?"
    print(f"Searching memory for: '{query}'")
    results = await cognee.search(query)
    print("Results:")
    for res in results:
        print(f" - {res}")


if __name__ == "__main__":
    asyncio.run(main())
