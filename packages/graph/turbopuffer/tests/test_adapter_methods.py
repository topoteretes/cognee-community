"""Per-method contract tests for TurbopufferGraphAdapter.

Mirrors cognee/tests/integration/infrastructure/graph/test_kuzu_adapter.py so the
TurboPuffer graph adapter is held to the same behavior as the reference adapters,
plus the v1-specific boundaries (no multi-hop BFS, no Cypher, no random-offset
pagination, traversal-dependent metrics return -1).

These run against a real TurboPuffer namespace (integration tier).
"""

import pytest

from conftest import demo_node_tuples, demo_edge_tuples, requires_turbopuffer

pytestmark = [pytest.mark.asyncio, requires_turbopuffer]


# --- is_empty --------------------------------------------------------------


async def test_is_empty_on_fresh_db(adapter):
    assert await adapter.is_empty() is True


async def test_not_empty_after_add(adapter):
    await adapter.add_nodes(demo_node_tuples())
    assert await adapter.is_empty() is False


# --- nodes: add / get / delete --------------------------------------------


async def test_add_and_get_node(adapter):
    nodes = demo_node_tuples()
    await adapter.add_nodes([nodes[0]])  # Alice

    result = await adapter.get_node("Alice")
    assert result is not None
    assert result["id"] == "Alice"
    assert result["name"] == "Alice"
    assert result["type"] == "Person"


async def test_get_node_missing_returns_none(adapter):
    assert await adapter.get_node("Nonexistent") is None


async def test_add_and_get_nodes(adapter):
    nodes = demo_node_tuples()
    await adapter.add_nodes(nodes)

    ids = [n[0] for n in nodes]
    results = await adapter.get_nodes(ids)
    assert {r["id"] for r in results} == set(ids)


async def test_add_nodes_is_idempotent_last_wins(adapter):
    await adapter.add_nodes([("Alice", {"name": "Alice", "type": "Person", "description": "v1"})])
    await adapter.add_nodes([("Alice", {"name": "Alice", "type": "Person", "description": "v2"})])

    result = await adapter.get_node("Alice")
    assert result["description"] == "v2"
    # Upsert dedup: still a single node.
    nodes, _ = await adapter.get_graph_data()
    assert len([n for n in nodes if n[0] == "Alice"]) == 1


async def test_delete_node(adapter):
    await adapter.add_nodes(demo_node_tuples())
    await adapter.delete_node("CheshireCat")
    assert await adapter.get_node("CheshireCat") is None


async def test_delete_nodes_clears_graph(adapter):
    nodes = demo_node_tuples()
    await adapter.add_nodes(nodes)
    await adapter.delete_nodes([n[0] for n in nodes])
    assert await adapter.is_empty() is True


async def test_delete_node_cascades_to_touching_edges(adapter):
    """No FK in TurboPuffer — the adapter must manually delete edges that
    reference a deleted node (both as source and as target)."""
    await adapter.add_nodes(demo_node_tuples())
    await adapter.add_edges(demo_edge_tuples())

    # MadHatter is source of (MadHatter->CheshireCat) and target of (Alice->MadHatter).
    await adapter.delete_node("MadHatter")

    assert await adapter.has_edge("Alice", "MadHatter", "meets") is False
    assert await adapter.has_edge("MadHatter", "CheshireCat", "knows") is False


# --- edges: add / has ------------------------------------------------------


async def test_add_and_has_edge(adapter):
    await adapter.add_nodes(demo_node_tuples())
    await adapter.add_edge("Alice", "WhiteRabbit", "follows", {})
    assert await adapter.has_edge("Alice", "WhiteRabbit", "follows") is True
    assert await adapter.has_edge("Alice", "WhiteRabbit", "loathes") is False


async def test_add_edges_with_uuid_ids(adapter):
    """Real cognee node ids are UUIDs; the synthetic edge id must stay within
    TurboPuffer's 64-byte id limit (regression: "{src}__{rel}__{tgt}" with two
    UUIDs is 83 bytes and 400s)."""
    import uuid

    s, t = str(uuid.uuid4()), str(uuid.uuid4())
    await adapter.add_nodes(
        [
            (s, {"name": "Alice", "type": "Entity"}),
            (t, {"name": "White Rabbit", "type": "Entity"}),
        ]
    )
    await adapter.add_edge(s, t, "follows", {})

    assert await adapter.has_edge(s, t, "follows") is True
    edges = await adapter.get_edges(s)
    assert any(e[0] == s and e[1] == t and e[2] == "follows" for e in edges)


async def test_add_edges_batch_and_has_edges(adapter):
    await adapter.add_nodes(demo_node_tuples())
    edges = demo_edge_tuples()
    await adapter.add_edges(edges)

    check = [(s, t, r) for (s, t, r, _) in edges]
    existing = await adapter.has_edges(check)
    assert len(existing) == len(edges)


# --- single-hop reads ------------------------------------------------------


async def test_get_edges(adapter):
    await adapter.add_nodes(demo_node_tuples())
    await adapter.add_edges(demo_edge_tuples())

    edges = await adapter.get_edges("Alice")
    # Alice: out follows->WhiteRabbit, meets->MadHatter; in summons<-QueenOfHearts
    assert len(edges) >= 2


async def test_get_neighbors(adapter):
    await adapter.add_nodes(demo_node_tuples())
    await adapter.add_edges(demo_edge_tuples())

    neighbors = await adapter.get_neighbors("Alice")
    neighbor_ids = {n["id"] for n in neighbors}
    assert "WhiteRabbit" in neighbor_ids
    assert "MadHatter" in neighbor_ids


async def test_get_connections_returns_triples(adapter):
    await adapter.add_nodes(demo_node_tuples())
    await adapter.add_edges(demo_edge_tuples())

    connections = await adapter.get_connections("Alice")
    assert len(connections) >= 1
    for conn in connections:
        assert len(conn) == 3  # (source_node, relationship_info, target_node)
        source_node, rel_info, target_node = conn
        assert "id" in source_node and "id" in target_node


# --- neighborhood: depth=1 only (NO BFS in v1) -----------------------------


async def test_get_neighborhood_depth1_ok(adapter):
    await adapter.add_nodes(demo_node_tuples())
    await adapter.add_edges(demo_edge_tuples())

    nodes, edges = await adapter.get_neighborhood(["Alice"], depth=1)
    node_ids = {n[0] for n in nodes}
    # Seed + direct neighbors present.
    assert "Alice" in node_ids
    assert {"WhiteRabbit", "MadHatter"} & node_ids
    assert len(edges) >= 1


async def test_get_neighborhood_depth2_raises(adapter):
    """v1 explicitly does not implement multi-hop BFS."""
    await adapter.add_nodes(demo_node_tuples())
    await adapter.add_edges(demo_edge_tuples())

    with pytest.raises(NotImplementedError):
        await adapter.get_neighborhood(["Alice"], depth=2)


# --- whole-graph reads -----------------------------------------------------


async def test_get_graph_data(adapter):
    nodes = demo_node_tuples()
    edges = demo_edge_tuples()
    await adapter.add_nodes(nodes)
    await adapter.add_edges(edges)

    got_nodes, got_edges = await adapter.get_graph_data()
    assert len(got_nodes) == len(nodes)
    assert len(got_edges) == len(edges)


async def test_get_filtered_graph_data_by_type(adapter):
    await adapter.add_nodes(demo_node_tuples())
    await adapter.add_edges(demo_edge_tuples())

    persons, _ = await adapter.get_filtered_graph_data([{"type": ["Person"]}])
    assert {n[0] for n in persons} == {"Alice", "WhiteRabbit", "QueenOfHearts", "MadHatter"}

    animals, _ = await adapter.get_filtered_graph_data([{"type": ["Animal"]}])
    assert {n[0] for n in animals} == {"CheshireCat"}


# --- nodeset subgraph ------------------------------------------------------


# get_nodeset_subgraph matches nodes whose `type` == node_type.__name__ (the
# Postgres/Neo4j contract). The demo nodes are type "Person", so the probe type
# must be named "Person".
class Person:
    pass


async def test_get_nodeset_subgraph_or(adapter):
    await adapter.add_nodes(demo_node_tuples())
    await adapter.add_edges(demo_edge_tuples())

    # node_name matches the node NAME (not id); demo names have spaces.
    nodes, edges = await adapter.get_nodeset_subgraph(
        node_type=Person, node_name=["Alice", "Mad Hatter"], node_name_filter_operator="OR"
    )
    node_ids = {n[0] for n in nodes}
    # Both named primaries are returned (by id), plus their 1-hop neighbors.
    assert {"Alice", "MadHatter"} <= node_ids


async def test_get_nodeset_subgraph_and(adapter):
    await adapter.add_nodes(demo_node_tuples())
    await adapter.add_edges(demo_edge_tuples())

    # AND mode: keep only neighbors connected to ALL named primaries.
    # QueenOfHearts is the sole node linked to both Alice (summons->Alice) and
    # WhiteRabbit (serves->QueenOfHearts); MadHatter links to Alice only.
    nodes, edges = await adapter.get_nodeset_subgraph(
        node_type=Person, node_name=["Alice", "White Rabbit"], node_name_filter_operator="AND"
    )
    node_ids = {n[0] for n in nodes}
    assert {"Alice", "WhiteRabbit", "QueenOfHearts"} <= node_ids
    assert "MadHatter" not in node_ids


# --- metrics: counts real, traversal-dependent ones are -1 -----------------


async def test_get_graph_metrics_counts(adapter):
    nodes = demo_node_tuples()
    edges = demo_edge_tuples()
    await adapter.add_nodes(nodes)
    await adapter.add_edges(edges)

    metrics = await adapter.get_graph_metrics()
    assert metrics["num_nodes"] == len(nodes)
    assert metrics["num_edges"] == len(edges)


async def test_get_graph_metrics_traversal_fields_are_sentinel(adapter):
    """Connectivity-dependent metrics need traversal we don't do in v1 — they
    must return the -1 sentinel, exactly like the Postgres adapter does for
    diameter/clustering."""
    await adapter.add_nodes(demo_node_tuples())
    await adapter.add_edges(demo_edge_tuples())

    metrics = await adapter.get_graph_metrics(include_optional=True)
    for field in ("num_connected_components", "diameter", "avg_shortest_path_length",
                  "avg_clustering"):
        if field in metrics:
            assert metrics[field] == -1


# --- triplets: sequential ok, random offset rejected -----------------------


async def test_get_triplets_batch_sequential(adapter):
    await adapter.add_nodes(demo_node_tuples())
    edges = demo_edge_tuples()
    await adapter.add_edges(edges)

    triplets = await adapter.get_triplets_batch(offset=0, limit=10)
    assert len(triplets) == len(edges)
    for t in triplets:
        assert "start_node" in t
        assert "relationship_properties" in t
        assert "end_node" in t


async def test_get_triplets_batch_arbitrary_offset_raises(adapter):
    """TurboPuffer is cursor-paginated, not offset-paginated. A nonzero offset
    cannot be served efficiently, so v1 rejects it rather than scanning O(offset)."""
    await adapter.add_nodes(demo_node_tuples())
    await adapter.add_edges(demo_edge_tuples())

    with pytest.raises(NotImplementedError):
        await adapter.get_triplets_batch(offset=5, limit=10)


# --- unsupported surface ---------------------------------------------------


async def test_raw_query_raises(adapter):
    """No query language on TurboPuffer -> Cypher path is unsupported."""
    with pytest.raises(NotImplementedError):
        await adapter.query("MATCH (n) RETURN n", {})


# --- lifecycle -------------------------------------------------------------


async def test_delete_graph_empties_both_namespaces(adapter):
    await adapter.add_nodes(demo_node_tuples())
    await adapter.add_edges(demo_edge_tuples())
    assert await adapter.is_empty() is False

    await adapter.delete_graph()
    assert await adapter.is_empty() is True
    # edges gone too
    assert await adapter.has_edge("Alice", "WhiteRabbit", "follows") is False
