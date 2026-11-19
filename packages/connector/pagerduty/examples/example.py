"""Runnable example for PagerDuty data-source connector in Cognee."""

import asyncio
import os

import cognee

from cognee_community_connector_pagerduty import pagerduty_source


async def main():
    token = os.getenv("PAGERDUTY_API_KEY", "mock_pd_token_for_demo")
    print("Connecting to PagerDuty API...")

    # Configure PagerDuty source (fetches high-urgency or recent incidents + services)
    source = pagerduty_source(
        token=token,
        resources=["incidents", "services"],
        statuses=["triggered", "acknowledged", "resolved"],
        urgencies=["high"],
    )

    print("Adding PagerDuty source to Cognee...")
    await cognee.add(source)

    print("Running Cognify pipeline (extracting incident topologies & knowledge graph)...")
    await cognee.cognify()

    print("Searching Cognee memory for incident insights:")
    query = "What high-urgency incidents occurred recently and what were the root causes?"
    results = await cognee.search(query)
    for result in results:
        print(f"Result: {result}")


if __name__ == "__main__":
    asyncio.run(main())
