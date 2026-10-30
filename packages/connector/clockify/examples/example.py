import os
from cognee_community_connector_clockify import ClockifyConnector

def main():
    api_key = os.getenv("CLOCKIFY_API_KEY", "mock_clockify_api_key")
    print("=== Clockify Cognee Connector Demonstration ===")
    
    connector = ClockifyConnector(api_key=api_key)
    print("Fetching time logs and project documents...")
    documents = connector.fetch_documents(page_size=10)

    print(f"Retrieved {len(documents)} structured Cognee documents.\n")
    for doc in documents[:3]:
        print(f"Doc ID: {doc['id']}")
        print(f"Project: {doc['metadata']['project_name']}")
        print("Sample Text:\n" + "\n".join(doc['text'].splitlines()[:8]))
        print("-" * 50)

if __name__ == "__main__":
    main()
