"""Example usage of the Polar data connector for Cognee."""

import os

from cognee_community_connector_polar.polar import (
    PolarClient,
    RenderOptions,
    sync_polar_records,
)


def main():
    # 1. Provide your read-only Polar Organization Access Token
    token = os.environ.get("POLAR_ACCESS_TOKEN", "polar_oat_your_token_here")
    org_id = os.environ.get("POLAR_ORGANIZATION_ID", None)
    is_sandbox = os.environ.get("POLAR_SANDBOX", "false").lower() == "true"

    print("Initializing Polar connector...")
    client = PolarClient(access_token=token, is_sandbox=is_sandbox)

    # 2. Configure privacy options
    options = RenderOptions(
        include_customer_email=False,  # Exclude customer emails by default
        include_product_description=True,
    )

    # 3. Synchronize records into documents
    print("Syncing Polar customers, subscriptions, and products...")
    count = 0
    for doc in sync_polar_records(client, organization_id=org_id, options=options):
        count += 1
        print(f"[{count}] {doc['title']} (ID: {doc['id']})")
        print(f"    Type: {doc['type']}")
        print(f"    Sample Content:\n{doc['text'][:120]}...\n")

    print(f"Sync complete. Processed {count} Polar documents.")


if __name__ == "__main__":
    main()
