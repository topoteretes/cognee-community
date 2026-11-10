import asyncio
import os
import pathlib
from os import path

# Please provide an OpenAI API Key
# Keep CI/local env-provided keys intact and only default to empty if unset.
os.environ.setdefault("LLM_API_KEY", "")


async def main():
    # NOTE: Importing the register module lets cognee know it can use the Elasticsearch adapter
    # NOTE: The "noqa: F401" mark is to make sure the linter doesn't flag this as an unused import
    from cognee import SearchType, add, cognify, config, prune, search
    from cognee_community_vector_adapter_elasticsearch import register  # noqa: F401

    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee-system"))
    config.data_root_directory(path.join(system_path, ".cognee-data"))

    # Please provide your Elasticsearch URL and, if the cluster is secured, an API key
    config.set_vector_db_config(
        {
            "vector_db_provider": "elasticsearch",
            "vector_db_url": os.getenv("VECTOR_DB_URL", "http://localhost:9200"),
            "vector_db_key": os.getenv("VECTOR_DB_KEY", ""),
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
