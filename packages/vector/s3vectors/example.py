import asyncio
import os
import pathlib
from os import path

# NOTE: Importing the register module we let cognee know it can use the S3 Vectors vector adapter
# NOTE: The "noqa: F401" mark is to make sure the linter doesn't flag this as an unused import
from cognee_community_vector_adapter_s3vectors import register  # noqa: F401


async def main():
    from cognee import SearchType, add, cognify, config, prune, search
    from dotenv import load_dotenv

    load_dotenv()

    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee_system"))
    config.data_root_directory(path.join(system_path, ".data_storage"))

    config.set_relational_db_config(
        {
            "db_provider": "sqlite",
        }
    )
    config.set_vector_db_config(
        {
            "vector_db_provider": "s3vectors",
            "vector_dataset_database_handler": "s3vectors",
            # Optional; defaults to the cognee database name.
            "vector_db_name": os.getenv("S3VECTORS_BUCKET_NAME", ""),
            # Optional; leave both unset to use the standard AWS credential chain
            # (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars, shared config,
            # or an IAM role). The region comes from AWS_REGION / AWS_DEFAULT_REGION.
            "vector_db_username": os.getenv("S3VECTORS_ACCESS_KEY_ID", ""),
            "vector_db_password": os.getenv("S3VECTORS_SECRET_ACCESS_KEY", ""),
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
    Natural language processing (NLP) is an interdisciplinary
    subfield of computer science and information retrieval.
    """

    await add(text)

    await cognify()

    query_text = "Tell me about NLP"

    search_results = await search(query_type=SearchType.GRAPH_COMPLETION, query_text=query_text)

    for result_text in search_results:
        print(result_text)


if __name__ == "__main__":
    asyncio.run(main())
