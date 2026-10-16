"""Fetch GitLab documents and build searchable memory in a dedicated dataset."""

import asyncio
import os

import cognee

from cognee_community_connector_gitlab import gitlab_source


async def main():
    source = gitlab_source(
        token=os.environ["GITLAB_TOKEN"],
        base_url=os.environ.get("GITLAB_BASE_URL", "https://gitlab.com"),
        resource_types=["issues", "merge_requests", "wiki"],
    )
    await cognee.remember(
        source,
        dataset_name="gitlab",
        primary_key="id",
        write_disposition="merge",
        run_in_background=False,
    )


if __name__ == "__main__":
    asyncio.run(main())
