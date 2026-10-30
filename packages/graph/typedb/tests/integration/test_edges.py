"""Edge CRUD round-trips."""


async def test_has_edge_is_directional_and_label_aware(seeded):
    adapter = seeded.adapter
    assert await adapter.has_edge(seeded.ml, seeded.ai, "is_subset_of")
    assert not await adapter.has_edge(seeded.ai, seeded.ml, "is_subset_of")
    assert not await adapter.has_edge(seeded.ml, seeded.ai, "unrelated_label")


async def test_has_edges_returns_existing_tuples(seeded):
    existing = await seeded.adapter.has_edges(
        [
            (seeded.ml, seeded.ai, "is_subset_of", {}),
            (seeded.ai, seeded.ml, "is_subset_of", {}),  # wrong direction: absent
            (seeded.dl, seeded.ai, "related_to", {}),
        ]
    )
    assert existing == [
        (seeded.ml, seeded.ai, "is_subset_of"),
        (seeded.dl, seeded.ai, "related_to"),
    ]
    assert await seeded.adapter.has_edges([]) == []


async def test_edge_upsert_does_not_duplicate(seeded):
    adapter = seeded.adapter
    await adapter.add_edge(seeded.ml, seeded.ai, "is_subset_of", {"weight": 2})
    edges = await adapter.get_edges(seeded.ai)
    assert len(edges) == 2  # ml->ai (upserted), dl->ai


async def test_get_edges_is_anchor_first(seeded):
    # Cognee's format_edges keys on slot 1 as the neighbour, so the queried
    # node is always slot 0 regardless of the edge's true direction.
    edges = await seeded.adapter.get_edges(seeded.ml)
    triples = {(first, second, rel["relationship_name"]) for first, second, rel in edges}
    assert triples == {
        (seeded.ml, seeded.ai, "is_subset_of"),  # outgoing: ml -> ai
        (seeded.ml, seeded.dl, "is_subset_of"),  # incoming dl -> ml, anchor-first
    }


async def test_add_edges_skips_missing_endpoints(seeded):
    adapter = seeded.adapter
    await adapter.add_edges([(seeded.ml, "missing-node", "dangling", {})])
    assert await adapter.has_edges([(seeded.ml, "missing-node", "dangling", {})]) == []


async def test_edge_identity_survives_separator_characters_in_ids(adapter):
    for node_id in ("a|b", "c", "a", "b|c"):
        await adapter.add_node(node_id, {"name": node_id})
    await adapter.add_edge("a|b", "c", "r")
    await adapter.add_edge("a", "b|c", "r")
    assert await adapter.has_edge("a|b", "c", "r")
    assert await adapter.has_edge("a", "b|c", "r")
    assert not await adapter.has_edge("a", "c", "r")
    _, edges = await adapter.get_graph_data()
    assert len(edges) == 2
