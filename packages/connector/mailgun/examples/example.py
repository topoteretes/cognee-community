"""Example demonstrating Mailgun email telemetry & template ingestion into Cognee.

This script fetches sending domains, DNS records, delivery stats, templates,
and bounce failure logs from Mailgun, indexes them into Cognee memory via cognify(),
and queries delivery diagnostics and template configurations.
"""

from __future__ import annotations

import asyncio
import os

import cognee

from cognee_community_connector_mailgun import (
    MailgunConfig,
    mailgun_connector,
)

DATASET_NAME = "email_infrastructure"


async def main() -> None:
    # 1. Initialize connector configuration
    config = MailgunConfig(
        api_key=os.getenv("MAILGUN_API_KEY", "key-mock-example-12345"),
        is_eu_region=False,
        include_stats=True,
        include_templates=True,
        include_bounces=True,
    )

    print("Fetching Mailgun domains, deliverability analytics, and templates...")
    documents = await mailgun_connector(config)
    print(f"Retrieved {len(documents)} structured email telemetry documents.")

    # 2. Ingest documents into Cognee dataset
    print(f"\nAdding documents to Cognee dataset '{DATASET_NAME}'...")
    await cognee.add(documents, dataset_name=DATASET_NAME)

    # 3. Cognify into knowledge graph and vector indices
    print("Cognifying dataset to build email infrastructure & delivery graph...")
    await cognee.cognify(dataset_name=DATASET_NAME)

    # 4. Search and reason over email knowledge memory
    queries = [
        "What are our primary sending domains and are DKIM and SPF records active?",
        "What are the recent bounce rates and delivery error patterns?",
        "What email templates are available for user welcome and password resets?",
    ]

    for query in queries:
        print(f"\nQuery: '{query}'")
        results = await cognee.search(query, dataset_name=DATASET_NAME)
        for res in results:
            print(f"- {res}")


if __name__ == "__main__":
    asyncio.run(main())
