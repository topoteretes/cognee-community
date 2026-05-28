"""Example: Cognee with the pgGraph community graph adapter."""

import asyncio
import os

import cognee
from cognee.infrastructure.databases.graph import get_graph_engine

from cognee_community_graph_adapter_pggraph import register


async def main():
    register()

    cognee.config.set_graph_database_provider("pggraph")
    cognee.config.set_graph_db_config(
        {
            "graph_database_host": os.getenv("GRAPH_DATABASE_HOST", "localhost"),
            "graph_database_port": int(os.getenv("GRAPH_DATABASE_PORT", "5433")),
            "graph_database_name": os.getenv("GRAPH_DATABASE_NAME", "cognee"),
            "graph_database_username": os.getenv("GRAPH_DATABASE_USERNAME", "cognee"),
            "graph_database_password": os.getenv("GRAPH_DATABASE_PASSWORD", "cognee"),
        }
    )

    print("=== pgGraph adapter demo ===\n")
    graph = await get_graph_engine()
    print(f"Adapter class: {graph.__class__.__name__}")
    print(f"pgGraph ready: {getattr(graph, '_pggraph_ready', False)}")

    await graph.delete_graph()
    await graph.add_nodes(
        [
            ("turing", {"name": "Alan Turing", "type": "Person"}),
            ("bletchley", {"name": "Bletchley Park", "type": "Place"}),
            ("crypto", {"name": "Cryptography", "type": "Field"}),
        ]
    )
    await graph.add_edges(
        [
            ("turing", "bletchley", "worked_at", {}),
            ("turing", "crypto", "researched", {}),
        ]
    )

    if hasattr(graph, "build_graph"):
        print(f"build_graph: {await graph.build_graph()}")

    neighbors = await graph.get_neighbors("turing")
    print(f"Neighbors: {[n.get('name') for n in neighbors]}")

    nodes, edges = await graph.get_neighborhood(["turing"], depth=2)
    print(f"2-hop node ids: {[n[0] for n in nodes]}")
    print(f"Edges in subgraph: {len(edges)}")
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
