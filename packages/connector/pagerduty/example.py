import os
from cognee_community_connector_pagerduty import PagerDutyConnector

def main():
    api_token = os.environ.get("PAGERDUTY_API_TOKEN", "mock_pagerduty_api_token")
    print(f"Connecting to PagerDuty API using token: {api_token[:4]}...")

    connector = PagerDutyConnector(
        api_token=api_token,
        statuses=["triggered", "acknowledged"],
        urgencies=["high"],
        include_services=True
    )

    print("Fetching active high-urgency incidents and service registry...")
    try:
        documents = connector.fetch_documents(limit=5)
        print(f"Synthesized {len(documents)} document nodes for Cognee.")
        for doc in documents[:2]:
            print(f"\nDocument ID: {doc['id']}")
            print(f"Type: {doc['metadata']['type']}")
            print(f"Summary Preview:\n{doc['text'][:250]}...\n")
    except Exception as e:
        print(f"Demo completed (API call requires active network credentials): {e}")

if __name__ == "__main__":
    main()
