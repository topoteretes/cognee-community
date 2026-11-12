"""Example usage of the Zendesk Cognee Community Connector."""

import os
from cognee_community_connector_zendesk import ZendeskClient, zendesk_data

def main():
    print("--- Testing Zendesk Connector Client Configuration ---")

    subdomain = os.getenv("ZENDESK_SUBDOMAIN", "example-demo")
    email = os.getenv("ZENDESK_EMAIL", "support@example.com")
    api_token = os.getenv("ZENDESK_API_TOKEN", "mock_api_token")

    client = ZendeskClient(subdomain=subdomain, email=email, api_token=api_token)
    print(f"Configured Zendesk Client with Base URL: {client.base_url}")
    print("Zendesk connector ready for ingestion into Cognee.")

if __name__ == "__main__":
    main()
