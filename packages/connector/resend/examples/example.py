"""Runnable example demonstrating Resend ingestion into Cognee."""

import asyncio
import os
import sys

import cognee

from cognee_community_connector_resend import resend_source


async def main() -> None:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        print("RESEND_API_KEY environment variable not set.")
        print("Please set it to run against live Resend API.")
        sys.exit(0)

    print("Configuring Resend connector...")
    source = resend_source(api_key=api_key, include=("domains", "audiences"))

    print("Adding Resend documents to Cognee...")
    await cognee.add(source)

    print("Running Cognify pipeline...")
    await cognee.cognify()

    print("Searching for verified email domains...")
    results = await cognee.recall("Which email domains are configured in Resend?")
    for res in results:
        print(f"- {res}")


if __name__ == "__main__":
    asyncio.run(main())
