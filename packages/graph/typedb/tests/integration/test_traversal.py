"""Traversal reads: predecessors, successors, neighbors, connections."""


async def test_predecessors_and_successors(seeded):
    adapter = seeded.adapter
    predecessors = await adapter.get_predecessors(seeded.ai)
    assert {p["name"] for p in predecessors} == {"machine learning", "deep learning"}

    predecessors = await adapter.get_predecessors(seeded.ai, edge_label="is_subset_of")
    assert {p["name"] for p in predecessors} == {"machine learning"}

    successors = await adapter.get_successors(seeded.dl)
    assert {s["name"] for s in successors} == {"machine learning", "artificial intelligence"}


async def test_empty_edge_label_is_a_real_filter(seeded):
    # "" must filter on the empty label (matching nothing here), not fall
    # back to unfiltered like None does.
    assert await seeded.adapter.get_predecessors(seeded.ai, edge_label="") == []
    assert len(await seeded.adapter.get_predecessors(seeded.ai, edge_label=None)) == 2


async def test_get_neighbors_combines_directions(seeded):
    neighbors = await seeded.adapter.get_neighbors(seeded.ml)
    assert {n["name"] for n in neighbors} == {"deep learning", "artificial intelligence"}


async def test_get_connections_orders_triples_by_direction(seeded):
    connections = await seeded.adapter.get_connections(seeded.ai)
    assert len(connections) == 2  # both incoming: ml->ai, dl->ai
    for _source, relationship, target in connections:
        assert target["id"] == seeded.ai
        assert relationship["relationship_name"] in {"is_subset_of", "related_to"}


async def test_self_loop_is_reported_once(seeded):
    adapter = seeded.adapter
    await adapter.add_edge(seeded.ai, seeded.ai, "self_referential")
    connections = await adapter.get_connections(seeded.ai)
    self_loops = [
        (source, rel, target)
        for source, rel, target in connections
        if source["id"] == seeded.ai and target["id"] == seeded.ai
    ]
    assert len(self_loops) == 1
