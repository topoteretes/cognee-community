"""
Example: Using NeuG as a hybrid (graph + vector) backend for Cognee.

NeuG is an embedded database — no server to run. Graph and vector live in one
file resolved from NEUG_DB_PATH (default: <cognee data root>/databases/neug_db).

Prerequisites:
  - pip install cognee-community-hybrid-adapter-neug
  - an LLM API key (LLM_API_KEY, OpenAI by default)
"""

import asyncio
import os
import pathlib
from os import path

# NeuG is single-tenant (graph + vector share one embedded file, no per-dataset
# isolation), so backend access control must be off. Set it before importing
# cognee so the config singletons pick it up.
os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] = "false"

from cognee import SearchType, add, cognify, config, prune, search  # noqa: E402

# Importing the register module lets Cognee know about the NeuG adapter.
from cognee_community_hybrid_adapter_neug import register  # noqa: E402,F401


async def main():
    # Set up local directories
    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee_system"))
    config.data_root_directory(path.join(system_path, ".cognee_data"))

    # Optional: pin the embedded database file explicitly.
    # os.environ["NEUG_DB_PATH"] = path.join(system_path, ".cognee_data", "neug_db")

    # Configure databases
    config.set_relational_db_config(
        {
            "db_provider": "sqlite",
        }
    )

    # Configure NeuG as both the graph and the vector provider (same name).
    config.set_vector_db_config(
        {
            "vector_db_provider": "neug",
        }
    )
    config.set_graph_db_config(
        {
            "graph_database_provider": "neug",
        }
    )

    # Optional: Clean previous data
    await prune.prune_data()
    await prune.prune_system(metadata=True)

    # Add and process content
    await add("""
    Natural language processing (NLP) is an interdisciplinary
    subfield of computer science and information retrieval.
    """)

    await add("""
    Machine learning is a subset of artificial intelligence that
    provides systems the ability to automatically learn and improve
    from experience without being explicitly programmed.
    """)

    await cognify()

    # Search using graph completion
    query_text = "Tell me about NLP"
    search_results = await search(
        query_type=SearchType.GRAPH_COMPLETION,
        query_text=query_text,
    )

    for result in search_results:
        print("\nSearch result:\n" + result)


if __name__ == "__main__":
    asyncio.run(main())
