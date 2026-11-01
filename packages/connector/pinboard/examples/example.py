import os
from cognee_community_connector_pinboard import pinboard_source

def main():
    """
    Example demonstrating ingestion of Pinboard bookmarks and notes into Cognee.
    """
    token = os.getenv("PINBOARD_API_TOKEN", "username:YOUR_SECRET_TOKEN")

    print("Connecting to Pinboard API...")
    try:
        documents = pinboard_source(auth_token=token)
        print(f"Retrieved {len(documents)} bookmarks for Cognee memory ingestion:\n")
        for doc in documents[:3]:
            print(f"- Title: {doc['title']}")
            print(f"  URL: {doc['metadata']['url']}")
            print(f"  Tags: {doc['metadata']['tags']}\n")
    except Exception as e:
        print(f"Note: Running with dummy credentials: {e}")

if __name__ == "__main__":
    main()
