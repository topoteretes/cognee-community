import asyncio
import os
import pathlib
from os import path

# Please provide an API Key if needed
os.environ.setdefault("LLM_API_KEY", "")


async def main():
    from cognee import SearchType, add, cognify, config, prune, search

    # NOTE: Importing the register module we let cognee know it can use the helixdb adapter
    # NOTE: The "noqa: F401" mark is to make sure the linter doesn't flag this as an unused import
    from cognee_community_hybrid_adapter_helixdb import register  # noqa: F401

    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee_system"))
    config.data_root_directory(path.join(system_path, ".data_storage"))

    config.set_vector_db_config(
        {
            "vector_db_provider": "helixdb",
            "vector_db_url": "localhost",
            "vector_db_port": 6969,
        }
    )

    # Please provide your HelixDB instance configuration
    config.set_graph_db_config(
        {
            "graph_database_provider": "helixdb",
            "graph_database_url": "localhost",
            "graph_database_port": 6969,
        }
    )
    await prune.prune_data()
    await prune.prune_system(metadata=True)

    await add("""
    Natural language processing (NLP) is an interdisciplinary
    subfield of computer science and information retrieval.
    """)

    await add("""
    Sandwiches are best served toasted with cheese, ham, mayo,
    lettuce, mustard, and salt & pepper.
    """)

    await cognify()

    query_text = "Tell me about NLP"

    search_results = await search(query_type=SearchType.GRAPH_COMPLETION, query_text=query_text)

    for result_text in search_results:
        print(result_text)


if __name__ == "__main__":
    asyncio.run(main())
