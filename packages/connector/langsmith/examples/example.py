import asyncio
import os
import cognee
from cognee_community_connector_langsmith import langsmith_source

async def main():
    api_key = os.environ.get("LANGSMITH_API_KEY", "mock_langsmith_key")

    print("Connecting LangSmith source into Cognee...")
    source = langsmith_source(
        api_key=api_key,
        resources=["prompts", "datasets"],
    )

    await cognee.prune.prune_data()
    await cognee.remember(
        source,
        dataset_name="langsmith_metadata",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    print("LangSmith prompts and datasets indexed into Cognee successfully!")

if __name__ == "__main__":
    asyncio.run(main())
