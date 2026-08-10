import asyncio
import os
import pathlib
from os import path

# NOTE: Importing the register module we let cognee know it can use the TopK vector adapter
# NOTE: The "noqa: F401" mark is to make sure the linter doesn't flag this as an unused import
from cognee_community_vector_adapter_topk import register  # noqa: F401


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
            "vector_db_provider": "topk",
            "vector_db_key": os.getenv("TOPK_API_KEY", ""),
            "vector_dataset_database_handler": "topk",
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
