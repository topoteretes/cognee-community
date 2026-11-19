"""Run from the package directory after installing it; see README.md."""

import asyncio
import os

import cognee

from cognee_community_connector_jira_service_management import (
    jira_service_management_source,
    list_service_desks,
)


async def main():
    connection = {
        "base_url": os.environ["JSM_URL"],
        "email": os.environ["JSM_EMAIL"],
        "api_token": os.environ["JSM_API_TOKEN"],
    }
    if not os.environ.get("JSM_SERVICE_DESK_IDS"):
        for desk in list_service_desks(**connection):
            print(desk["id"], desk.get("projectName", desk.get("projectKey", "")))
        print("Set JSM_SERVICE_DESK_IDS to comma-separated IDs from this list, then run again.")
        return

    source = jira_service_management_source(
        **connection,
        service_desk_ids=[value.strip() for value in os.environ["JSM_SERVICE_DESK_IDS"].split(",")],
    )
    dataset = os.environ.get("JSM_DATASET", "jsm_requests")
    await cognee.remember(
        source,
        dataset_name=dataset,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        run_in_background=False,
    )
    print(
        await cognee.search(
            query_text="Summarize the requests and their SLA status.",
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=[dataset],
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
