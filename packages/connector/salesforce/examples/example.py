"""Salesforce connector demo — sync CRM records into memory.

Demonstrates connecting to Salesforce via OAuth 2.0 or Username-Password flow,
ingesting standard CRM objects (Account, Opportunity, Case, Chatter FeedItem)
as structured relational records, and querying memory with Cognee.

Setup:
    1. pip install "cognee-community-connector-salesforce"
    2. Set environment variables in .env (see .env.example)
    3. Run: python examples/example.py
"""

import asyncio
import os

import cognee
from dotenv import load_dotenv

from cognee_community_connector_salesforce import salesforce_source

load_dotenv()

DATASET_NAME = "salesforce_crm"

REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,  # disable row cap so orphan_cleanup inspects whole corpus
}


async def main():
    instance_url = os.getenv("SALESFORCE_INSTANCE_URL")
    client_id = os.getenv("SALESFORCE_CLIENT_ID")
    client_secret = os.getenv("SALESFORCE_CLIENT_SECRET")
    refresh_token = os.getenv("SALESFORCE_REFRESH_TOKEN")

    username = os.getenv("SALESFORCE_USERNAME")
    password = os.getenv("SALESFORCE_PASSWORD")
    security_token = os.getenv("SALESFORCE_SECURITY_TOKEN")

    if not (instance_url or username):
        print(
            "Please configure Salesforce credentials in your environment or .env file.\n"
            "See packages/connector/salesforce/examples/.env.example for details."
        )
        return

    # 1. Build the Salesforce dlt source
    source = salesforce_source(
        instance_url=instance_url,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        username=username,
        password=password,
        security_token=security_token,
        objects=["Account", "Opportunity", "Case", "FeedItem"],
    )

    # 2. Ingest into Cognee memory
    print("=== Syncing Salesforce records to Cognee ===")
    result = await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        **REMEMBER_KWARGS,
    )
    print("Ingestion result:", result)

    # 3. Query the ingested CRM data
    print("\n=== Querying Salesforce memory ===")
    answer = await cognee.search(
        query_text="List the accounts and active opportunities with their current stages",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Answer:\n", answer)

    # 4. Demonstrate incremental re-sync
    print("\n=== Running incremental sync (delta only) ===")
    # Re-running uses persisted state['Object_last_sync'] and emits getDeleted tombstones
    incremental_result = await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        **REMEMBER_KWARGS,
    )
    print("Incremental sync result:", incremental_result)


if __name__ == "__main__":
    asyncio.run(main())
