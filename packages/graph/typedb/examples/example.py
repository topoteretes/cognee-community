"""Example usage of the TypeDB community adapter for Cognee.

Requires a running TypeDB 3.12+ server (default: 127.0.0.1:1729) and an LLM
API key in the environment (see the repo README).
"""

import asyncio
import os
import pathlib

# Per-dataset TypeDB databases for cognee's backend access control (read at
# engine creation, so it must be set before cognee builds its config).
os.environ.setdefault("GRAPH_DATASET_DATABASE_HANDLER", "typedb")

import cognee

# NOTE: Importing register lets cognee know it can use the TypeDB graph adapter
from cognee_community_graph_adapter_typedb import register


async def main():
    # Configure cognee to use TypeDB
    cognee.config.set_graph_database_provider("typedb")
    register()

    # Set up your TypeDB connection (TypeDB 3.12+, default credentials shown)
    cognee.config.set_graph_db_config(
        {
            "graph_database_url": os.environ.get("GRAPH_DATABASE_URL", "127.0.0.1:1729"),
            "graph_database_username": os.environ.get("GRAPH_DATABASE_USERNAME", "admin"),
            "graph_database_password": os.environ.get("GRAPH_DATABASE_PASSWORD", "password"),
            # One TypeDB database per dataset (cognee's backend access control).
            "graph_dataset_database_handler": "typedb",
        }
    )

    # Optional: Set custom data and system directories
    system_path = pathlib.Path(__file__).parent
    cognee.config.system_root_directory(os.path.join(system_path, ".cognee_system"))
    cognee.config.data_root_directory(os.path.join(system_path, ".data_storage"))

    sample_data = [
        "TypeDB is a polymorphic database with a conceptual data model.",
        "TypeQL is TypeDB's declarative query language.",
        "Knowledge graphs represent entities and the relationships between them.",
        "Cognee builds AI memory by combining knowledge graphs with vector search.",
    ]

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    print("Adding data to Cognee...")
    await cognee.add(sample_data, "typedb_knowledge")

    print("Processing data with Cognee...")
    await cognee.cognify(["typedb_knowledge"])

    print("Searching for insights...")
    search_results = await cognee.search(
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        query_text="How does cognee use knowledge graphs?",
    )

    print(f"Found {len(search_results)} insights:")
    for index, result in enumerate(search_results, 1):
        print(f"{index}. {result}")

    # A look at what landed in TypeDB: the dataset's graph lives in its own
    # database, and raw TypeQL goes through the adapter's query() with values
    # passed as a `given` row.
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.modules.data.methods import get_datasets_by_name
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    dataset = (await get_datasets_by_name(["typedb_knowledge"], user.id))[0]
    async with set_database_global_context_variables(dataset.id, dataset.owner_id):
        graph = await get_graph_engine()
    print(f"\nTypeDB database: {graph.database_name}")
    for row in await graph.query(
        "match $n isa node, has node-type $t; reduce $count = count groupby $t;"
    ):
        print(f"  {row['t']:>20}  {row['count']} nodes")
    entities = await graph.query(
        "given $type: string;\n"
        "match $n isa node, has node-type == $type, has name $name;\n"
        "select $name; sort $name; limit 5;",
        {"type": "Entity"},
    )
    print("  first entities:", ", ".join(row["name"] for row in entities))


if __name__ == "__main__":
    asyncio.run(main())
