"""Example usage of the Jira Cognee Community Connector."""

import os
from cognee_community_connector_jira import JiraClient, jira_data

def main():
    print("--- Testing Jira Connector Client Configuration ---")

    domain = os.getenv("JIRA_DOMAIN", "acme-example")
    email = os.getenv("JIRA_EMAIL", "engineer@acme.com")
    api_token = os.getenv("JIRA_API_TOKEN", "mock_api_token")

    client = JiraClient(domain=domain, email=email, api_token=api_token)
    print(f"Configured Jira Client with Base URL: {client.base_url}")
    print("Jira connector ready for ingestion into Cognee.")

if __name__ == "__main__":
    main()
