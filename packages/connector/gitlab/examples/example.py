"""Fetch GitLab documents and build searchable memory in a dedicated dataset."""

import asyncio
import os

import cognee

from cognee_community_connector_gitlab import gitlab_source


async def main():
    # Optional comma-separated IDs or group/project paths; unset syncs every
    # project the token owner is a member of.
    selection = os.environ.get("GITLAB_PROJECT_IDS")
    project_ids = (
        [int(ref) if ref.strip().isdigit() else ref.strip() for ref in selection.split(",")]
        if selection
        else None
    )
    source = gitlab_source(
        token=os.environ["GITLAB_TOKEN"],
        base_url=os.environ.get("GITLAB_BASE_URL", "https://gitlab.com"),
        resource_types=["issues", "merge_requests", "comments", "wiki"],
        project_ids=project_ids,
    )
    await cognee.remember(
        source,
        dataset_name="gitlab",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        run_in_background=False,
    )


if __name__ == "__main__":
    asyncio.run(main())
