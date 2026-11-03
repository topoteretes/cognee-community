"""Example of using the Hypothesis connector with Cognee."""

import os
from cognee_community_connector_hypothesis import hypothesis_source


def main():
    api_token = os.environ.get("HYPOTHESIS_API_TOKEN")
    username = os.environ.get("HYPOTHESIS_USERNAME", "myuser")

    print(f"Initializing Hypothesis connector for user: {username}")
    source = hypothesis_source(
        api_token=api_token,
        username=username,
        max_results=50,
    )
    print("Hypothesis dlt source configured successfully:", source)


if __name__ == "__main__":
    main()
