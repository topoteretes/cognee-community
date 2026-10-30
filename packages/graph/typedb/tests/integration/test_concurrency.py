"""Concurrent writers on one artifact: TypeDB must flag the conflict ([STC2])
so the retried transaction sees the other writer's result.

These cover the mainstream cognee path the contract tests do not: entity nodes
have deterministic ids, data items are processed concurrently, so two folded
attaches with different (or the same) source refs routinely land on the same
pre-existing node. Two adapter instances stand in for two workers.
"""

import asyncio
from uuid import uuid4

import pytest
from cognee.infrastructure.databases.provenance import make_source_ref_key, make_source_run_ref
from support import ADDRESS, Concept

from cognee_community_graph_adapter_typedb import TypeDBAdapter

TRIALS = 5


@pytest.fixture
async def second(adapter):
    """A second adapter instance on the same database (own driver, own lock)."""
    other = TypeDBAdapter(graph_database_url=ADDRESS, database_name=adapter.database_name)
    yield other
    await other.close()


async def _ref_links(adapter, node_id):
    """(key, position) of the node's source-ref links, in attach order."""
    rows = await adapter.query(
        "given $id: string; match $n isa node, has node-id == $id;"
        " $l isa sourced-from, links (artifact: $n, ref: $r), has position $p;"
        " $r has source-ref-key $k; select $k, $p;",
        {"id": node_id},
    )
    return sorted((row["k"], row["p"]) for row in rows)


async def test_concurrent_folded_attach_on_existing_node_keeps_all_keys(adapter, second):
    for trial in range(TRIALS):
        node = Concept(name=f"shared-{trial}")
        node_id = str(node.id)
        await adapter.add_nodes([node])  # pre-existing: the fold is an update, not a put
        run = uuid4()
        keys = [make_source_ref_key(uuid4(), uuid4()) for _ in range(2)]

        await asyncio.gather(
            adapter.add_nodes([node], source_ref_key=keys[0], pipeline_run_id=str(run)),
            second.add_nodes([node], source_ref_key=keys[1], pipeline_run_id=str(run)),
        )

        snap = (await adapter.get_node_delete_data([node_id]))[node_id]
        assert sorted(snap.source_ref_keys) == sorted(keys)
        assert sorted(snap.source_run_refs) == sorted(make_source_run_ref(run, k) for k in keys)
        links = await _ref_links(adapter, node_id)
        assert [k for k, _ in links] == sorted(snap.source_ref_keys)  # one link per key
        assert len({p for _, p in links}) == len(links)  # distinct positions


async def test_concurrent_explicit_attach_across_instances_keeps_all_keys(adapter, second):
    for trial in range(TRIALS):
        node = Concept(name=f"shared-{trial}")
        node_id = str(node.id)
        await adapter.add_nodes([node])
        keys = [make_source_ref_key(uuid4(), uuid4()) for _ in range(4)]

        await asyncio.gather(
            adapter.attach_node_source_refs([node_id], [keys[0]], str(uuid4())),
            second.attach_node_source_refs([node_id], [keys[1]], str(uuid4())),
            adapter.attach_node_source_refs([node_id], [keys[2]], None),
            second.remove_node_source_refs([node_id], [keys[3]]),  # no-op racer
        )

        snap = (await adapter.get_node_delete_data([node_id]))[node_id]
        assert sorted(snap.source_ref_keys) == sorted(keys[:3])
        links = await _ref_links(adapter, node_id)
        assert [k for k, _ in links] == sorted(snap.source_ref_keys)  # one link per key
        assert len({p for _, p in links}) == len(links)  # distinct positions


async def test_concurrent_property_mutations_all_land(adapter, second):
    for trial in range(TRIALS):
        node = Concept(name=f"shared-{trial}", belongs_to_set=["Dev", "Keep"])
        node_id = str(node.id)
        await adapter.add_nodes([node])

        await asyncio.gather(
            adapter.set_node_feedback_weights({node_id: 0.9}),
            second.set_node_truth_state({node_id: {"truth_alignment": ["x"], "truth_epoch": 2}}),
            adapter.remove_belongs_to_set_tags(["Dev"], node_ids=[node_id]),
        )

        stored = await adapter.get_node(node_id)
        assert stored["feedback_weight"] == 0.9
        assert stored["truth_epoch"] == 2
        assert stored["belongs_to_set"] == ["Keep"]


async def test_concurrent_attach_of_the_same_key_links_it_once(adapter, second):
    """Two instances attaching the SAME key to one node at once: the ref
    entity is put by both, the loser of the artifact conflict re-reads, and
    the key ends up linked exactly once with one run ref (Model A)."""
    for trial in range(TRIALS):
        node = Concept(name=f"shared-{trial}")
        node_id = str(node.id)
        await adapter.add_nodes([node])
        key, run_a, run_b = make_source_ref_key(uuid4(), uuid4()), uuid4(), uuid4()

        await asyncio.gather(
            adapter.add_nodes([node], source_ref_key=key, pipeline_run_id=str(run_a)),
            second.attach_node_source_refs([node_id], [key], str(run_b)),
        )

        snap = (await adapter.get_node_delete_data([node_id]))[node_id]
        assert snap.source_ref_keys == [key]
        assert len(snap.source_run_refs) == 1  # only the run that attached it first
        assert [k for k, _ in await _ref_links(adapter, node_id)] == [key]


async def test_delete_racing_an_attach_leaves_no_dangling_links(adapter, second):
    """Deleting a node on one instance while another attaches to it: either
    order is fine, but no link may survive without its node."""
    for trial in range(TRIALS):
        node = Concept(name=f"doomed-{trial}")
        node_id = str(node.id)
        await adapter.add_nodes([node])
        keys = [make_source_ref_key(uuid4(), uuid4()) for _ in range(40)]  # a longer attach

        await asyncio.gather(
            adapter.delete_nodes([node_id]),
            second.attach_node_source_refs([node_id], keys, str(uuid4())),
        )

        assert not await adapter.has_node(node_id)
        for link in ("sourced-from", "run-attached"):
            dangling = await adapter.query(
                f"match $l isa {link}; not {{ $l links (artifact: $x); }}; select $l;"
            )
            assert dangling == []
