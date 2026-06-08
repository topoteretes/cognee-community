import asyncio
import os
import pathlib

# NOTE: Importing the register module we let cognee know it can use the Weaviate vector adapter
# NOTE: The "noqa: F401" mark is to make sure the linter doesn't flag this as an unused import
from cognee_community_vector_adapter_weaviate import register  # noqa: F401


async def main():
    from cognee import SearchType, add, cognify, config, prune, search

    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(os.path.join(system_path, ".cognee_system"))
    config.data_root_directory(os.path.join(system_path, ".cognee_data"))

    config.set_relational_db_config(
        {
            "db_provider": "sqlite",
        }
    )
    config.set_vector_db_config(
        {
            "vector_db_provider": "weaviate",
            "vector_db_url": os.getenv(
                "WEAVIATE_API_URL",
                "",
            ),
            "vector_db_key": os.getenv("WEAVIATE_API_KEY", ""),
        }
    )
    config.set_graph_db_config(
        {
            "graph_database_provider": "ladybug",
        }
    )

    await prune.prune_data()
    await prune.prune_system(metadata=True)

    text = """
    Weaviate is an open-source vector database that stores both objects and vectors.
    It allows for combining vector search with structured filtering.
    Weaviate can be deployed in the cloud, on-premise, or embedded in your application.
    """

    await add(text)

    await cognify()

    query_text = "Tell me about Weaviate vector database"

    search_results = await search(query_type=SearchType.GRAPH_COMPLETION, query_text=query_text)

    for result_text in search_results:
        print(result_text)


if __name__ == "__main__":
    asyncio.run(main())
