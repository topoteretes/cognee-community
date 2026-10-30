import os
from cognee_community_connector_atcoder import AtCoderConnector

def main():
    username = os.getenv("ATCODER_USERNAME", "chokudai")
    print(f"Initializing AtCoder Connector for user: {username}")
    
    connector = AtCoderConnector(username=username)
    documents = connector.fetch_documents()
    
    print(f"Successfully retrieved {len(documents)} Cognee memory documents.")
    for doc in documents[:3]:
        print("---")
        print("ID:", doc["id"])
        print("Metadata:", doc["metadata"])
        print("Preview:\n", doc["text"][:200])

if __name__ == "__main__":
    main()
