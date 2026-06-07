"""Inspect the knowledge graph after running the pipeline.

Two views:
  1. Backend-agnostic (via cognee's graph engine): counts, metrics, a sample of
     nodes/edges. Works for ANY configured graph backend.
  2. Raw TurboPuffer (optional): lists the *_graph_node / *_graph_edge namespaces
     and dumps a sample of rows straight from TurboPuffer. Only relevant when the
     turbopuffer backend is in use.

Run the same backend selection as example.py (GRAPH_DATABASE_PROVIDER, keys).

    python examples/inspect_graph.py
    GRAPH_DATABASE_PROVIDER=turbopuffer TURBOPUFFER_API_KEY=... \
    TURBOPUFFER_REGION=gcp-us-east4 python examples/inspect_graph.py --raw
"""

import asyncio
import os
import sys

import cognee


async def inspect_via_cognee():
    provider = os.getenv("GRAPH_DATABASE_PROVIDER", "").lower()
    if provider == "turbopuffer":
        from cognee_community_graph_adapter_turbopuffer import register

        register()
        cognee.config.set_graph_database_provider("turbopuffer")

    from cognee.infrastructure.databases.graph import get_graph_engine

    engine = await get_graph_engine()

    nodes, edges = await engine.get_graph_data()
    print(f"\n=== graph via cognee ({provider or 'default'}) ===")
    print(f"nodes: {len(nodes)}   edges: {len(edges)}")

    print("\n-- sample nodes --")
    for node_id, props in nodes[:10]:
        print(f"  {node_id}  name={props.get('name')!r}  type={props.get('type')!r}")

    print("\n-- sample edges --")
    for edge in edges[:10]:
        # EdgeData is (source_id, target_id, relationship_name, properties)
        print(f"  {edge[0]} --{edge[2]}--> {edge[1]}")

    print("\n-- metrics --")
    metrics = await engine.get_graph_metrics(include_optional=True)
    for k, v in metrics.items():
        print(f"  {k}: {v}")


async def inspect_raw_turbopuffer():
    """Dump rows straight from TurboPuffer namespaces (filter-only lookup)."""
    import turbopuffer

    client = turbopuffer.Turbopuffer(
        api_key=os.environ["TURBOPUFFER_API_KEY"],
        region=os.getenv("TURBOPUFFER_REGION", "gcp-us-east4"),
    )

    print("\n=== raw TurboPuffer namespaces ===")
    for ns_handle in client.namespaces():
        name = getattr(ns_handle, "id", None) or getattr(ns_handle, "name", str(ns_handle))
        if not (name.endswith("_graph_node") or name.endswith("_graph_edge")):
            continue
        ns = client.namespace(name)
        resp = ns.query(rank_by=("id", "asc"), top_k=10, include_attributes=True)
        rows = resp.rows or []
        print(f"\n-- {name}  (showing {len(rows)}) --")
        for row in rows:
            print(f"  id={row.id}  {row.model_extra}")


async def main():
    await inspect_via_cognee()
    if "--raw" in sys.argv:
        await inspect_raw_turbopuffer()


if __name__ == "__main__":
    asyncio.run(main())
