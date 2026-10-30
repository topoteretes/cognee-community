"""Node CRUD round-trips."""

from uuid import UUID, uuid4

import pytest
from cognee.infrastructure.databases.provenance import (
    EdgeIdentity,
    make_source_ref_key,
    make_source_run_ref,
)
from support import Concept


async def test_empty_database(adapter):
    assert await adapter.is_empty()
    assert not await adapter.has_node("no-such-node")
    assert await adapter.get_nodes([]) == []
    assert await adapter.get_node("no-such-node") is None


async def test_add_and_get_nodes(adapter):
    ai = Concept(name="artificial intelligence", description='the "broad" field')
    ml = Concept(name="machine learning")
    await adapter.add_nodes([ai, ml])

    assert not await adapter.is_empty()
    assert await adapter.has_node(str(ai.id))

    node = await adapter.get_node(str(ai.id))
    assert node["id"] == str(ai.id)
    assert node["name"] == "artificial intelligence"
    assert node["description"] == 'the "broad" field'  # quote round-trip

    nodes = await adapter.get_nodes([str(ai.id), str(ml.id)])
    assert {n["name"] for n in nodes} == {"artificial intelligence", "machine learning"}


async def test_add_nodes_upserts_without_duplicating(adapter):
    ml = Concept(name="machine learning")
    await adapter.add_nodes([ml])
    await adapter.add_nodes([Concept(id=ml.id, name="machine learning", description="updated")])

    nodes = await adapter.get_nodes([str(ml.id)])
    assert len(nodes) == 1
    assert nodes[0]["description"] == "updated"


async def test_add_node_from_id_and_properties(adapter):
    await adapter.add_node("plain-id", {"name": "plain", "kind": "manual"})
    node = await adapter.get_node("plain-id")
    assert node["id"] == "plain-id"
    assert node["kind"] == "manual"


async def test_add_nodes_with_duplicate_ids_in_one_batch(adapter):
    first = Concept(name="first")
    second = Concept(id=first.id, name="second")
    await adapter.add_nodes([first, second])
    nodes = await adapter.get_nodes([str(first.id)])
    assert len(nodes) == 1
    assert nodes[0]["name"] == "second"  # last row wins


async def test_provenance_stamps_accumulate_and_survive_unstamped_upserts(seeded):
    """Each stamped write links the node to its run ref entity, and a
    provenance-less re-add leaves the existing links alone."""
    adapter = seeded.adapter
    ml = seeded.concepts["ml"]

    async def stamps():
        rows = await adapter.query(
            "given $id: string; match $n isa node, has node-id == $id;"
            " $l isa run-attached, links (artifact: $n, run: $rr);"
            " $rr has source-run-ref $r; select $r;",
            {"id": seeded.ml},
        )
        return {row["r"] for row in rows}

    first = make_source_run_ref(UUID(seeded.run), seeded.key)
    assert await stamps() == {first}

    await adapter.add_nodes([Concept(id=ml.id, name="machine learning")])  # unstamped
    assert await stamps() == {first}

    other_key, other_run = make_source_ref_key(uuid4(), uuid4()), uuid4()
    await adapter.add_nodes(
        [Concept(id=ml.id, name="machine learning")],
        source_ref_key=other_key,
        pipeline_run_id=str(other_run),
    )
    assert await stamps() == {first, make_source_run_ref(other_run, other_key)}


async def test_add_nodes_rejects_malformed_source_ref_key(adapter):
    with pytest.raises(ValueError):
        await adapter.add_nodes([Concept(name="x")], source_ref_key="ds:test")


async def test_created_at_mirrors_payload_and_updated_at_moves(adapter):
    """The TypeDB created-at attribute mirrors the DataPoint payload's own
    created_at (epoch ms); updated-at is the write time."""
    import asyncio

    node = Concept(name="timestamps")

    async def stamps():
        rows = await adapter.query(
            "given $id: string; match $a isa node-id == $id; $n isa node, has $a,"
            " has created-at $c, has updated-at $u; select $c, $u;",
            {"id": str(node.id)},
        )
        assert len(rows) == 1
        return rows[0]["c"], rows[0]["u"]

    await adapter.add_nodes([node])
    created, updated = await stamps()
    assert created == node.created_at
    assert updated >= created

    await asyncio.sleep(0.01)
    await adapter.add_nodes([node])  # same object: same created_at payload
    created_after, updated_after = await stamps()
    assert created_after == node.created_at
    assert updated_after > updated

    fresh = Concept(id=node.id, name="timestamps", description="rebuilt")
    await adapter.add_nodes([fresh])  # new object for the same id: follows its payload
    created_fresh, _ = await stamps()
    assert created_fresh == fresh.created_at


async def test_extract_node_aliases_get_node(seeded):
    node = await seeded.adapter.extract_node(seeded.ai)
    assert node["name"] == "artificial intelligence"
    nodes = await seeded.adapter.extract_nodes([seeded.ml, seeded.dl])
    assert len(nodes) == 2


async def test_delete_node_cascades_to_edges(seeded):
    adapter = seeded.adapter
    await adapter.delete_node(seeded.ml)
    assert not await adapter.has_node(seeded.ml)
    assert not await adapter.has_edge(seeded.dl, seeded.ml, "is_subset_of")
    assert await adapter.has_node(seeded.dl)  # neighbours survive
    assert await adapter.has_edge(seeded.dl, seeded.ai, "related_to")


async def _link_count(adapter) -> int:
    """Provenance links of both kinds."""
    refs = await adapter.query("match $l isa sourced-from; reduce $c = count;")
    runs = await adapter.query("match $l isa run-attached; reduce $c = count;")
    return refs[0]["c"] + runs[0]["c"]


async def test_delete_nodes_cascades_links_for_connected_sets_and_self_loops(seeded):
    """The delete planner passes connected sets; a self-loop and a duplicated
    id must not trip the cascade, and every provenance link of a deleted
    node or edge goes with it."""
    adapter = seeded.adapter
    await adapter.add_edge(seeded.dl, seeded.dl, "self_loop")
    await adapter.attach_edge_source_refs(
        [EdgeIdentity(seeded.dl, seeded.dl, "self_loop")], [seeded.key], seeded.run
    )
    before = await _link_count(adapter)
    assert before == 2 * (3 + 3 + 1)  # ref + run link for three nodes, three edges, the loop

    await adapter.delete_nodes([seeded.dl, seeded.ml, seeded.dl])  # connected pair, dup id

    assert not await adapter.has_node(seeded.dl) and not await adapter.has_node(seeded.ml)
    assert await adapter.has_node(seeded.ai)
    assert await _link_count(adapter) == 2  # only ai's own ref and run links remain
    assert await adapter.find_nodes_by_source_ref(seeded.key) == [seeded.ai]


async def test_delete_paths_remove_provenance_links(seeded):
    adapter = seeded.adapter
    assert await _link_count(adapter) == 12

    await adapter.delete_edge_triples([EdgeIdentity(seeded.ml, seeded.ai, "is_subset_of")])
    assert await _link_count(adapter) == 10

    await adapter.remove_connection_to_successors_of([seeded.dl], "related_to")
    assert await _link_count(adapter) == 8

    await adapter.delete_graph()
    assert await _link_count(adapter) == 0
    assert await adapter.find_nodes_by_source_ref(seeded.key) == []
    # Ref entities with nothing left to link are swept with the graph.
    for entity in ("source-ref", "run-ref"):
        refs = await adapter.query(f"match $r isa {entity}; reduce $c = count;")
        assert refs[0]["c"] == 0
