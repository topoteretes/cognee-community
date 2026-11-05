"""Example usage of Arize Phoenix data-source connector for Cognee."""

import os
from cognee_community_connector_phoenix import (
    PhoenixClient,
    phoenix_data,
    format_trace_to_markdown,
)

def main():
    base_url = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006")
    project = os.getenv("PHOENIX_PROJECT_NAME", "default")
    api_key = os.getenv("PHOENIX_API_KEY")

    print(f"Connecting to Arize Phoenix at: {base_url} (Project: {project})")

    # Ingest traces using generator
    count = 0
    for doc in phoenix_data(
        base_url=base_url,
        project_name=project,
        api_key=api_key,
        limit=10,
    ):
        count += 1
        print(f"\n--- Ingested Trace #{count}: {doc['id']} ---")
        print(f"Node sets: {doc['metadata']['node_sets']}")
        print(f"Text Preview (first 150 chars):\n{doc['text'][:150]}...")

    if count == 0:
        print("\nNo traces returned. Ensure Phoenix is running or populated with evaluation spans.")

if __name__ == "__main__":
    main()
