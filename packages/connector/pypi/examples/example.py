"""Example usage of the PyPI Data-Source Connector with Cognee."""

from cognee_community_connector_pypi import PyPIConnector


def main():
    connector = PyPIConnector()

    packages_to_index = ["cognee", "fastapi", "pydantic"]
    print(f"Fetching metadata and documentation for: {packages_to_index}...")

    documents = connector.fetch_documents(packages_to_index)

    for doc in documents:
        print("\n" + "=" * 60)
        print(f"ID: {doc['id']}")
        print(f"Title: {doc['title']}")
        print(f"Metadata: {doc['metadata']}")
        print(f"Snippet (first 250 chars):\n{doc['text'][:250]}...")

    # Integration with Cognee:
    # import asyncio
    # import cognee
    # async def ingest():
    #     for doc in documents:
    #         await cognee.add(doc["text"])
    #     await cognee.cognify()
    #     results = await cognee.search("What are the key dependencies of fastapi?")
    #     print(results)
    # asyncio.run(ingest())


if __name__ == "__main__":
    main()
