import os

from cognee_community_connector_gitbook.gitbook import gitbook_source


def main():
    api_token = os.environ["GITBOOK_API_TOKEN"]
    space_id = os.environ["GITBOOK_SPACE_ID"]

    source = gitbook_source(
        api_token=api_token,
        space_id=space_id,
    )

    for page in source:
        print(f"Page: {page['title']}")
        print(page["content"])
        print("-" * 80)


if __name__ == "__main__":
    main()
