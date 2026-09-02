import asyncio
import os
import pathlib
from os import path

# Please provide an OpenAI API Key
os.environ["LLM_API_KEY"] = ""


async def main():
    # NOTE: Importing the register module lets cognee know it can use the
    # s3vectors adapter.
    import cognee_community_vector_adapter_s3vectors.register  # noqa: F401
    from cognee import SearchType, add, cognify, config, prune, search

    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee-system"))
    config.data_root_directory(path.join(system_path, ".cognee-data"))

    # S3 Vectors has no URL/API key -- auth is via the standard boto3
    # credential chain (environment variables, shared credentials file, IAM
    # role, etc.). `vector_db_name` maps onto the S3 vector bucket name
    # (created automatically if it doesn't already exist).
    config.set_vector_db_config(
        {
            "vector_db_provider": "s3vectors",
            "vector_db_name": "my-s3-vector-bucket",
        }
    )

    config.set_relational_db_config(
        {
            "db_provider": "sqlite",
        }
    )

    config.set_graph_db_config(
        {
            "graph_database_provider": "networkx",
        }
    )

    await prune.prune_data()
    await prune.prune_system(metadata=True)

    text = """
    Natural language processing (NLP) is an interdisciplinary
    subfield of computer science and information retrieval.
    """

    await add(text)

    await cognify()

    query_text = "Tell me about NLP"

    search_results = await search(query_type=SearchType.GRAPH_COMPLETION, query_text=query_text)

    for result_text in search_results:
        print("\nSearch result: \n" + result_text)


if __name__ == "__main__":
    asyncio.run(main())
