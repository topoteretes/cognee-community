import asyncio
import os

import cognee

from cognee_community_connector_typeform import typeform_source


async def main():
    token = os.environ.get("TYPEFORM_API_KEY") or "mock_token"

    source = typeform_source(token=token)

    dataset_name = "typeform_feedback_dataset"

    await cognee.remember(
        source,
        dataset_name=dataset_name,
    )

    query = "What feedback did customers provide about our pricing?"
    results = await cognee.recall(
        query,
        dataset_name=dataset_name,
    )

    print(f"Recall results for '{query}':")
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
