import os
import pathlib

import cognee
from cognee.infrastructure.files.storage import get_storage_config
from cognee.modules.search.operations import get_history
from cognee.modules.search.types import SearchType
from cognee.modules.users.methods import get_default_user
from cognee.shared.logging_utils import get_logger

# NOTE: Importing the register module we let cognee know it can use the helixdb adapter
# NOTE: The "noqa: F401" mark is to make sure the linter doesn't flag this as an unused import
from cognee_community_hybrid_adapter_helixdb import register  # noqa: F401

logger = get_logger()


async def check_helixdb_connection():
    """Check if HelixDB is available at localhost:6969"""
    try:
        import helix

        client = helix.Client(local=True, port=6969)
        # Try a basic query to check connectivity
        client.query("is_empty_check", {})
        return True
    except Exception as e:
        logger.warning(f"HelixDB not available at localhost:6969: {e}")
        return False


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

    # Check that we did not accidentally use any default value for limit
    # in vector search along the way (like 5, 10, or 15)
    assert len(result) > 15


async def main():
    # Check if HelixDB is available
    if not await check_helixdb_connection():
        print("HelixDB is not available at localhost:6969")
        print("   To run this test, start HelixDB:")
        print("   helix dashboard start --host localhost --helix-port 6969")
        print("   Skipping HelixDB test...")
        return

    print("HelixDB connection successful, running test...")

    # Configure HelixDB as the graph database provider
    cognee.config.set_graph_db_config(
        {
            "graph_database_url": "localhost",
            "graph_database_port": 6969,
            "graph_database_provider": "helixdb",
        }
    )

    # Configure HelixDB as the vector database provider too since it's a hybrid adapter
    cognee.config.set_vector_db_config(
        {
            "vector_db_url": "localhost",
            "vector_db_port": 6969,
            "vector_db_provider": "helixdb",
        }
    )

    data_directory_path = str(
        pathlib.Path(
            os.path.join(pathlib.Path(__file__).parent, ".data_storage/test_helixdb")
        ).resolve()
    )
    cognee.config.data_root_directory(data_directory_path)
    cognee_directory_path = str(
        pathlib.Path(
            os.path.join(pathlib.Path(__file__).parent, ".cognee_system/test_helixdb")
        ).resolve()
    )
    cognee.config.system_root_directory(cognee_directory_path)

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    dataset_name = "artificial_intelligence"

    ai_text_file_path = os.path.join(
        pathlib.Path(__file__).parent.parent.parent.parent,
        "test_data/artificial-intelligence.pdf",
    )
    await cognee.add([ai_text_file_path], dataset_name)

    text = """A large language model (LLM) is a language model notable for its ability to achieve
    general-purpose language generation and other natural language processing tasks such as
    classification. LLMs acquire these abilities by learning statistical relationships from
    text documents during a computationally intensive self-supervised and semi-supervised training
    process. LLMs can be used for text generation, a form of generative AI, by taking an input text
    and repeatedly predicting the next token or word. LLMs are artificial neural networks. The
    largest and most capable, as of March 2024, are built with a decoder-only transformer-based
    architecture while some recent implementations are based on other architectures, such as
    recurrent neural network variants and Mamba (a state space model). Up to 2020, fine tuning was
    the only way a model could be adapted to be able to accomplish specific tasks. Larger sized
    models, such as GPT-3, however, can be prompt-engineered to achieve similar results.[6]
    They are thought to acquire knowledge about syntax, semantics and "ontology" inherent in human
    language corpora, but also inaccuracies and biases present in the corpora. Some notable LLMs
    are OpenAI's GPT series of models (e.g., GPT-3.5 and GPT-4, used in ChatGPT and Microsoft
    Copilot), Google's PaLM and Gemini (the latter of which is currently used in the chatbot of the
    same name), xAI's Grok, Meta's LLaMA family of open-source models, Anthropic's Claude models,
    Mistral AI's open source models, and Databricks' open source DBRX.
    """

    await cognee.add([text], dataset_name)

    await cognee.cognify([dataset_name])

    from cognee.infrastructure.databases.vector import get_vector_engine

    vector_engine = get_vector_engine()
    random_node = (
        await vector_engine.search(
            collection_name="Entity_name", query_text="AI", include_payload=True
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

    search_results = await cognee.search(query_type=SearchType.CHUNKS, query_text=random_node_name)
    assert len(search_results) != 0, "The search results list is empty."
    print("\n\nExtracted chunks are:\n")
    for result in search_results:
        print(f"{result}\n")

    search_results = await cognee.search(
        query_type=SearchType.SUMMARIES, query_text=random_node_name
    )
    assert len(search_results) != 0, "Query related summaries don't exist."
    print("\nExtracted summaries are:\n")
    for result in search_results:
        print(f"{result}\n")

    user = await get_default_user()
    history = await get_history(user.id)

    assert len(history) == 6, "Search history is not correct."

    # Assert local data files are cleaned properly
    await cognee.prune.prune_data()
    data_root_directory = get_storage_config()["data_root_directory"]
    assert not os.path.isdir(data_root_directory), "Local data files are not deleted"

    # Assert relational, vector and graph databases have been cleaned properly
    await cognee.prune.prune_system(metadata=True)

    # For HelixDB, check if the graph is empty as vector data is stored alongside graph data
    from cognee.infrastructure.databases.graph import get_graph_engine

    graph_engine = await get_graph_engine()
    graph_data = await graph_engine.get_graph_data()
    nodes, edges = graph_data
    assert len(nodes) == 0 and len(edges) == 0, "HelixDB graph database is not empty"

    from cognee.infrastructure.databases.relational import get_relational_engine

    assert not os.path.exists(get_relational_engine().db_path), (
        "SQLite relational database is not empty"
    )

    await test_vector_engine_search_none_limit()

    print("HelixDB test completed successfully!")
    print("   - Data ingestion worked")
    print("   - Cognify processing worked")
    print("   - Search operations worked")
    print("   - Cleanup worked")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main(), debug=True)
