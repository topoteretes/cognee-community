"""Ingest LogRocket session metadata and issue reports into Cognee."""

import asyncio
import os

import cognee

from cognee_community_connector_logrocket import logrocket_source


async def main() -> None:
    organization_id = os.environ.get("LOGROCKET_ORGANIZATION_ID")
    project_id = os.environ.get("LOGROCKET_PROJECT_ID")
    if not organization_id or not project_id or not os.environ.get("LOGROCKET_API_KEY"):
        print(
            "Set LOGROCKET_API_KEY, LOGROCKET_ORGANIZATION_ID, LOGROCKET_PROJECT_ID, "
            "and LLM_API_KEY first."
        )
        return

    await cognee.remember(
        logrocket_source(
            organization_id=organization_id,
            project_id=project_id,
            resources=("sessions", "issues"),
        ),
        dataset_name="logrocket",
        max_rows_per_table=0,
    )
    result = await cognee.search(
        query_text="What issues affected users recently?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["logrocket"],
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
