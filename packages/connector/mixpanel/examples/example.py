import asyncio
import os

import cognee

from cognee_community_connector_mixpanel import mixpanel_source


async def main():
    project_id = os.environ.get("MIXPANEL_PROJECT_ID") or "123456"
    secret = os.environ.get("MIXPANEL_SERVICE_ACCOUNT_SECRET") or "mock_secret"

    source = mixpanel_source(
        project_id=project_id,
        service_account_secret=secret,
        include_schemas=True,
        include_cohorts=True,
        include_reports=True,
    )

    dataset_name = "mixpanel_product_analytics"

    await cognee.remember(
        source,
        dataset_name=dataset_name,
    )

    query = "What user properties are tracked in our signup and checkout events?"
    results = await cognee.recall(
        query,
        dataset_name=dataset_name,
    )

    print(f"Recall results for '{query}':")
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
