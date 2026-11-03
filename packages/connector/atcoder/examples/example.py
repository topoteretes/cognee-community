"""Example usage of the AtCoder Cognee Community Connector."""

import json
from cognee_community_connector_atcoder import AtCoderClient, atcoder_data

def main():
    print("--- Testing AtCoder Client Live Discovery ---")
    client = AtCoderClient(min_interval_seconds=1.0)

    # Test with renowned competitive programmer 'chokudai' (AtCoder Founder)
    handle = "chokudai"
    print(f"Fetching public data for handle: {handle}")
    data = client.fetch_user_data(handle, max_submissions=50)

    print("\n[Profile Summary]")
    print(data["profile"]["text"])

    print(f"\n[Problems Attempted]: {len(data['problems'])}")
    if data["problems"]:
        p = data["problems"][0]
        print(f"Sample Problem: {p['title']} (Difficulty: {p['difficulty']})")
        print(f"Solved: {p['is_solved']} | Attempts: {p['attempts_count']}")

    print(f"\n[Contests Tracked]: {len(data['contests'])}")
    if data["contests"]:
        c = data["contests"][-1]
        print(f"Latest Contest: {c['contest_name']} | Rating: {c['new_rating']}")

    print("\n✅ AtCoder connector live discovery successful!")

if __name__ == "__main__":
    main()
