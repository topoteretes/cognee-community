"""
Example: Using ArcadeDB as a hybrid (graph + vector) backend for Cognee.

Prerequisites:
  - ArcadeDB running on localhost:2480
  - pip install cognee-community-hybrid-adapter-arcadedb
"""

import asyncio
import os
import pathlib
from os import path

from cognee import config, prune, add, cognify, search, SearchType

# Importing the register module lets Cognee know about the ArcadeDB adapter
from cognee_community_hybrid_adapter_arcadedb import register  # noqa: F401


async def main():
    # Set up local directories
    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee_system"))
    config.data_root_directory(path.join(system_path, ".cognee_data"))

    # Configure databases
    config.set_relational_db_config({
        "db_provider": "sqlite",
    })

    # Configure ArcadeDB as both vector and graph database
    arcadedb_url = os.getenv("ARCADEDB_URL", "localhost")
    arcadedb_user = os.getenv("ARCADEDB_USERNAME", "root")
    arcadedb_pass = os.getenv("ARCADEDB_PASSWORD", "playwithdata")

    config.set_vector_db_config({
        "vector_db_provider": "arcadedb",
        "vector_db_url": arcadedb_url,
        "vector_db_port": int(os.getenv("ARCADEDB_HTTP_PORT", "2480")),
        "vector_db_username": arcadedb_user,
        "vector_db_password": arcadedb_pass,
    })
    config.set_graph_db_config({
        "graph_database_provider": "arcadedb",
        "graph_database_url": arcadedb_url,
        "graph_database_port": int(os.getenv("ARCADEDB_HTTP_PORT", "2480")),
        "graph_database_username": arcadedb_user,
        "graph_database_password": arcadedb_pass,
    })

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
