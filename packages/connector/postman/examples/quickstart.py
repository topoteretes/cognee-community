"""Minimal foreground sync using your configured Cognee stores and models."""

import asyncio
import os

import cognee

from cognee_community_connector_postman import postman_source


async def main():
    dataset = os.environ.get("POSTMAN_DATASET", "postman_docs")
    source = postman_source(
        api_key=os.environ["POSTMAN_API_KEY"],
        workspace_id=os.environ["POSTMAN_WORKSPACE_ID"],
        resource_name="postman_docs",
        collection_ids=os.environ["POSTMAN_COLLECTION_UIDS"].split(","),
    )
    await cognee.remember(
        source,
        dataset_name=dataset,
        run_in_background=False,
        dlt_config={"write_disposition": "replace", "primary_key": "id"},
    )


if __name__ == "__main__":
    asyncio.run(main())
