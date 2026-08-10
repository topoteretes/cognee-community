import os
import pathlib

import cognee
from cognee.infrastructure.files.storage import get_storage_config
from cognee.modules.search.operations import get_history
from cognee.modules.search.types import SearchType
from cognee.modules.users.methods import get_default_user
from cognee.shared.logging_utils import get_logger

# NOTE: Importing the register module we let cognee know it can use the TopK vector adapter
# NOTE: The "noqa: F401" mark is to make sure the linter doesn't flag this as an unused import
from cognee_community_vector_adapter_topk import register  # noqa: F401

logger = get_logger()


async def test_getting_of_documents(dataset_name_1):
    # Test getting of documents for search per dataset
    from cognee.modules.data.methods import get_datasets_by_name
    from cognee.modules.users.permissions.methods import get_document_ids_for_user

    user = await get_default_user()
    datasets = await get_datasets_by_name(dataset_name_1, user.id)
    document_ids = await get_document_ids_for_user(user.id, [dataset.id for dataset in datasets])
    assert len(document_ids) == 1, (
        f"Number of expected documents doesn't match {len(document_ids)} != 1"
    )

    # Test getting of documents for search when no dataset is provided
    user = await get_default_user()
    document_ids = await get_document_ids_for_user(user.id)
    assert len(document_ids) == 2, (
        f"Number of expected documents doesn't match {len(document_ids)} != 2"
    )


async def test_vector_engine_search_none_limit():
    file_path_quantum = os.path.join(
        pathlib.Path(__file__).parent.parent.parent.parent, "test_data/Quantum_computers.txt"
    )

    file_path_nlp = os.path.join(
        pathlib.Path(__file__).parent.parent.parent.parent,
        "test_data/Natural_language_processing.txt",
    )

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    await cognee.add(file_path_quantum)

    await cognee.add(file_path_nlp)

    await cognee.cognify()

    query_text = "Tell me about Quantum computers"

    from cognee.infrastructure.databases.vector import get_vector_engine

    vector_engine = get_vector_engine()

    collection_name = "Entity_name"

    query_vector = (await vector_engine.embedding_engine.embed_text([query_text]))[0]

    result = await vector_engine.search(
        collection_name=collection_name, query_vector=query_vector, limit=None
    )

    assert len(result) > 15


async def main():
    from dotenv import load_dotenv

    load_dotenv()

    cognee.config.set_relational_db_config(
        {
            "db_provider": "sqlite",
        }
    )
    cognee.config.set_vector_db_config(
        {
            "vector_db_provider": "topk",
            "vector_db_key": os.getenv("TOPK_API_KEY", ""),
            "vector_dataset_database_handler": "topk",
        }
    )
    cognee.config.set_graph_db_config(
        {
            "graph_database_provider": "ladybug",
        }
    )

    data_directory_path = str(
        pathlib.Path(
            os.path.join(pathlib.Path(__file__).parent, ".data_storage/test_topk")
        ).resolve()
    )
    cognee.config.data_root_directory(data_directory_path)
    cognee_directory_path = str(
        pathlib.Path(
            os.path.join(pathlib.Path(__file__).parent, ".cognee_system/test_topk")
        ).resolve()
    )
    cognee.config.system_root_directory(cognee_directory_path)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    dataset_name_1 = "natural_language"
    dataset_name_2 = "quantum"

    explanation_file_path_nlp = os.path.join(
        pathlib.Path(__file__).parent.parent.parent.parent,
        "test_data/Natural_language_processing.txt",
    )

    explanation_file_path_quantum = os.path.join(
        pathlib.Path(__file__).parent.parent.parent.parent, "test_data/Quantum_computers.txt"
    )

    await cognee.add([explanation_file_path_nlp], dataset_name_1)

    await cognee.add([explanation_file_path_quantum], dataset_name_2)

    await cognee.cognify([dataset_name_2, dataset_name_1])

    from cognee.infrastructure.databases.vector import get_vector_engine

    await test_getting_of_documents(dataset_name_1)

    vector_engine = get_vector_engine()
    random_node = (
        await vector_engine.search(
            collection_name="Entity_name", query_text="Quantum computer", include_payload=True
        )
    )[0]
    random_node_name = random_node.payload["text"]

    search_results = await cognee.search(
        query_type=SearchType.GRAPH_COMPLETION, query_text=random_node_name
    )
    assert len(search_results) != 0, "The search results list is empty."
    print("\n\nExtracted sentences are:\n")
    for result in search_results:
        print(f"{result}\n")

    search_results = await cognee.search(
        query_type=SearchType.CHUNKS, query_text=random_node_name, datasets=[dataset_name_2]
    )
    assert len(search_results) != 0, "The search results list is empty."
    print("\n\nExtracted chunks are:\n")
    for result in search_results:
        print(f"{result}\n")

    graph_completion = await cognee.search(
        query_type=SearchType.GRAPH_COMPLETION,
        query_text=random_node_name,
        datasets=[dataset_name_2],
    )
    assert len(graph_completion) != 0, "Completion result is empty."
    print("Completion result is:")
    print(graph_completion)

    search_results = await cognee.search(
        query_type=SearchType.SUMMARIES, query_text=random_node_name
    )
    assert len(search_results) != 0, "Query related summaries don't exist."
    print("\n\nExtracted summaries are:\n")
    for result in search_results:
        print(f"{result}\n")

    user = await get_default_user()
    history = await get_history(user.id)
    assert len(history) == 8, "Search history is not correct."

    await cognee.prune.prune_data()
    data_root_directory = get_storage_config()["data_root_directory"]
    assert not os.path.isdir(data_root_directory), "Local data files are not deleted"

    await cognee.prune.prune_system(metadata=True)
    tables_in_database = await vector_engine.get_collection_names()
    assert len(tables_in_database) == 0, "TopK database is not empty"

    await test_vector_engine_search_none_limit()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
