import os
from cognee_community_connector_leetcode import LeetCodeConnector

def main():
    username = os.environ.get("LEETCODE_USERNAME", "tourist")
    print(f"Connecting to LeetCode GraphQL for public user: {username}...")

    connector = LeetCodeConnector(
        username=username,
        include_problem_details=True
    )

    try:
        documents = connector.fetch_documents(limit=5)
        print(f"Synthesized {len(documents)} document nodes for Cognee.")
        for doc in documents[:2]:
            print(f"\nDocument ID: {doc['id']}")
            print(f"Problem: {doc['metadata']['title']} ({doc['metadata']['difficulty']})")
            print(f"Text Preview:\n{doc['text'][:250]}...\n")
    except Exception as e:
        print(f"Demo complete (requires public network connection to leetcode.com): {e}")

if __name__ == "__main__":
    main()
