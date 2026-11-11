"""Example usage of the Trello Cognee Community Connector."""

import os
from cognee_community_connector_trello import TrelloClient, trello_data

def main():
    print("--- Testing Trello Connector Integration ---")

    api_key = os.getenv("TRELLO_API_KEY", "demo_api_key")
    token = os.getenv("TRELLO_TOKEN", "demo_token")

    client = TrelloClient(api_key=api_key, token=token)
    print(f"Configured Trello Client with Base URL: {client.base_url}")

    # Inspect simulated board data schema
    print("\nTrello connector ready for ingestion with Cognee.")

if __name__ == "__main__":
    main()
