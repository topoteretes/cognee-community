import os
import asyncio
import pathlib
from os import path

# Please provide an OpenAI API Key
os.environ["LLM_API_KEY"]=""

async def main():
    from cognee import config, prune, add, cognify, search, SearchType
    # NOTE: Importing the register module we let cognee know it can use the Redis vector adapter
    from cognee_community_vector_adapter_redis import register

    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee-system"))
    config.data_root_directory(path.join(system_path, ".cognee-data"))

    # Please provide your Redis instance url
    config.set_vector_db_config({
        "vector_db_provider": "redis",
        "vector_db_url": "redis://localhost:6379",
        "vector_db_key": "",
    })

    await prune.prune_data()
    await prune.prune_system()

    await add("""
    Natural language processing (NLP) is an interdisciplinary
    subfield of computer science and information retrieval.
    """)

    await add("""
    Sandwhiches are best served toasted with cheese, ham, mayo,
    lettuce, mustard, and salt & pepper.          
    """)

    await cognify()

    query_text = "Tell me about NLP"

    search_results = await search(query_type=SearchType.GRAPH_COMPLETION, query_text=query_text)

    for result_text in search_results:
        print("\nSearch result: \n" + result_text)
    
if __name__ == "__main__":
    asyncio.run(main())
