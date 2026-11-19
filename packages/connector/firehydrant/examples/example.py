"""Example usage of FireHydrant data-source connector for Cognee."""

import os
from cognee_community_connector_firehydrant import (
    FireHydrantClient,
    firehydrant_data,
    format_incident_to_markdown,
)

def main():
    api_token = os.getenv("FIREHYDRANT_API_KEY", "mock_bot_token")
    print("Ingesting incidents from FireHydrant...")

    count = 0
    for doc in firehydrant_data(
        api_token=api_token,
        limit=10,
    ):
        count += 1
        print(f"\n--- Ingested Incident #{count}: {doc['id']} ---")
        print(f"Node Sets: {doc['metadata']['node_sets']}")
        print(f"Text Preview (first 150 chars):\n{doc['text'][:150]}...")

    if count == 0:
        print("\nNo incidents found or token not configured.")

if __name__ == "__main__":
    main()
