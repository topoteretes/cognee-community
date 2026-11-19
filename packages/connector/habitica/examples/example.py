import os
from cognee_community_connector_habitica import habitica_source

def main():
    """
    Example demonstrating ingestion of Habitica habits, dailies, and to-dos into Cognee.
    """
    user_id = os.getenv("HABITICA_USER_ID", "your-habitica-user-uuid")
    api_token = os.getenv("HABITICA_API_KEY", "your-habitica-api-token")

    print(f"Connecting to Habitica API for user: {user_id}...")
    try:
        documents = habitica_source(
            user_id=user_id,
            api_token=api_token
        )
        print(f"Successfully retrieved {len(documents)} Habitica tasks/habits for ingestion:\n")
        for doc in documents[:3]:
            print(f"- Title: {doc['title']}")
            print(f"  ID: {doc['id']}")
            print(f"  Type: {doc['metadata']['type']}")
            print(f"  Completed: {doc['metadata']['completed']}\n")
    except Exception as e:
        print(f"Note: Running with dummy credentials or API unavailable: {e}")

if __name__ == "__main__":
    main()
