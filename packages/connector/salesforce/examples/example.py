"""Salesforce connector demo - ask what happened around your accounts and cases.

Each Account, Contact, Opportunity and Case becomes one document with its fields plus the
text people write around it: case comments, tasks, and Chatter posts with their replies.
Each run reads only what changed; a new comment re-renders its case even though the case
itself did not change, and records deleted in Salesforce are forgotten.

Setup (see the README for each step):
1. In Setup, create an External Client App with OAuth enabled, the "api" scope, and the
   client credentials flow, Run As a dedicated read-only integration user.
2. Run:

       export SALESFORCE_LOGIN_URL="https://yourorg.my.salesforce.com"
       export SALESFORCE_CLIENT_ID="..."
       export SALESFORCE_CLIENT_SECRET="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_salesforce import salesforce_source

DATASET_NAME = "salesforce"


async def main() -> None:
    login_url = os.environ.get("SALESFORCE_LOGIN_URL")
    client_id = os.environ.get("SALESFORCE_CLIENT_ID")
    client_secret = os.environ.get("SALESFORCE_CLIENT_SECRET")
    if not (login_url and client_id and client_secret):
        print(
            "Set SALESFORCE_LOGIN_URL, SALESFORCE_CLIENT_ID and SALESFORCE_CLIENT_SECRET "
            "to run this example."
        )
        return

    source = salesforce_source(
        login_url=login_url, client_id=client_id, client_secret=client_secret
    )
    print("Syncing Salesforce records into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        self_improvement=False,  # no enrichment pass on every scheduled sync
    )
    stats = source.cognee_sync_stats
    print("Sync stats:", stats)
    if not stats.get("completed"):
        # a run stopped by its request budget or the org's API allowance still succeeds,
        # so a caller that cares has to look here
        print("This run did not finish; the next one resumes where it stopped.")

    answer = await cognee.search(
        query_text="Which open cases are putting a renewal at risk, and what was promised?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
