"""Foreground sync using the user's explicitly configured Cognee stores/models."""

import asyncio
import os

import cognee

from cognee_community_connector_jira import jira_source


def make_source():
    return jira_source(
        site_url=os.environ["JIRA_SITE_URL"],
        email=os.environ["JIRA_EMAIL"],
        api_token=os.environ["JIRA_API_TOKEN"],
        projects=os.environ["JIRA_PROJECTS"].split(","),
        resource_name=os.environ.get("JIRA_RESOURCE_NAME", "jira_docs"),
        jql=os.environ.get("JIRA_JQL"),
        missing_policy=os.environ["JIRA_MISSING_POLICY"],
    )


async def main():
    for _ in range(2):
        source = make_source()
        await cognee.remember(
            source,
            dataset_name=os.environ.get("JIRA_DATASET", "jira_docs"),
            run_in_background=False,
            self_improvement=False,
            dlt_config={"write_disposition": "replace", "primary_key": "id"},
            max_rows_per_table=0,
        )
        if source.cognee_sync_stats["extractions"]:
            return
    raise RuntimeError("No fresh Jira extraction after pending-load replay")


if __name__ == "__main__":
    asyncio.run(main())
