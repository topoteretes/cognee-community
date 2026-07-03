import asyncio
import os
import pathlib
from os import path

# NOTE: Importing the register module lets cognee use the ClickHouse vector adapter.
# NOTE: The "noqa: F401" mark keeps linters from flagging the side-effect import.
from cognee_community_vector_adapter_clickhouse import register  # noqa: F401


async def main():
    from cognee import SearchType, add, cognify, config, prune, search

    os.environ.setdefault("VECTOR_DATASET_DATABASE_HANDLER", "clickhouse")

    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee_system"))
    config.data_root_directory(path.join(system_path, ".data_storage"))

    config.set_relational_db_config({"db_provider": "sqlite"})
    config.set_vector_db_config(
        {
            "vector_db_provider": "clickhouse",
            "vector_db_url": os.getenv("CLICKHOUSE_URL", "http://127.0.0.1:8123/cognee"),
            "vector_db_key": os.getenv("CLICKHOUSE_KEY", ""),
        }
    )
    config.set_graph_db_config({"graph_database_provider": "ladybug"})

    await prune.prune_data()
    await prune.prune_system(metadata=True)

    text = """
    Natural language processing (NLP) is an interdisciplinary
    subfield of computer science and information retrieval.
    """

    await add(text)
    await cognify()

    search_results = await search(
        query_type=SearchType.GRAPH_COMPLETION,
        query_text="Tell me about NLP",
    )

    for result_text in search_results:
        print(result_text)


if __name__ == "__main__":
    asyncio.run(main())
