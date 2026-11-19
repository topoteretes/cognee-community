"""Example script demonstrating Gitee repository ingestion for Cognee."""

import os
from cognee_community_connector_gitee import GiteeConnector

def main():
    token = os.getenv("GITEE_ACCESS_TOKEN")
    connector = GiteeConnector(access_token=token)

    owner = "oschina"
    repo = "git-osc"
    print(f"Connecting to Gitee API v5 to extract data for {owner}/{repo}...")

    documents = connector.fetch_repository(
        owner=owner,
        repo=repo,
        include_issues=True,
        include_pulls=True,
        include_commits=True,
        include_releases=True,
        max_pages=2,
    )

    print(f"\nSuccessfully generated {len(documents)} Cognee documents:")
    for idx, doc in enumerate(documents[:5], 1):
        meta = doc.get("metadata", {})
        print(f"  {idx}. [{meta.get('item_type')}] {doc.get('name')} (ID: {doc.get('data_id')[:12]}...)")

if __name__ == "__main__":
    main()
