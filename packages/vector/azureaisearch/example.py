import sys
import asyncio
import pathlib
from os import path

######### Needed only for this example, not to be used in production code.
packages_module_path = path.join(pathlib.Path(__file__).parent.parent.parent.parent)
sys.path.append(packages_module_path)
#########


async def main():
    from cognee import config, prune, add, cognify, search, SearchType
    import packages.vector.azureaisearch.register

    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee-system"))
    config.data_root_directory(path.join(system_path, ".cognee-data"))

    config.set_relational_db_config({
        "db_provider": "sqlite",
    })
    config.set_vector_db_config({
        "vector_db_provider": "azureaisearch",
        "vector_db_url": "",
        "vector_db_key": "",
    })
    config.set_graph_db_config({
        "graph_database_provider": "networkx",
    })

    await prune.prune_data()
    await prune.prune_system()

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
