"""Concurrent-cognify shaped stress: several datasets written at once.

Cognee runs data items concurrently and, under access control, each dataset
has its own database. This drives four dataset databases, each written by
TWO adapter instances at once (as two workers, or a cached adapter and its
re-resolved replacement, would): every writer's batch spans several chunks
and folds a different source ref onto shared entity nodes, so the writers
conflict at commit on every chunk and must win through the retry budget.
Then every count and every provenance record is checked.
"""

import asyncio
from uuid import uuid4

import pytest
from cognee.infrastructure.databases.provenance import make_source_ref_key
from support import ADDRESS, Concept, drop_database, server_available

from cognee_community_graph_adapter_typedb import TypeDBAdapter

pytestmark = pytest.mark.skipif(not server_available(), reason=f"no TypeDB server at {ADDRESS}")

DATASETS = 4
WRITERS = 2  # adapter instances per dataset
NODES_PER_WRITER = 300  # several chunks per writer at the default chunk size
SHARED = 50  # entity nodes both writers of a dataset touch


async def _writer(adapter, nodes, edges, key, run):
    await adapter.add_nodes(nodes, source_ref_key=key, pipeline_run_id=run)
    await adapter.add_edges(edges, source_ref_key=key, pipeline_run_id=run)


async def test_concurrent_multi_dataset_writes_keep_counts_and_provenance():
    names = [f"cognee_test_{uuid4().hex[:12]}" for _ in range(DATASETS)]
    adapters = [
        [TypeDBAdapter(graph_database_url=ADDRESS, database_name=name) for _ in range(WRITERS)]
        for name in names
    ]
    try:
        plans = []
        for instances in adapters:
            shared = [Concept(name=f"shared {i}") for i in range(SHARED)]
            writers = []
            for w, instance in enumerate(instances):
                own = [Concept(name=f"w{w} {i}") for i in range(NODES_PER_WRITER - SHARED)]
                nodes = shared + own
                edges = [
                    (str(nodes[i].id), str(nodes[(i * 7 + 1) % len(nodes)].id), "links", {})
                    for i in range(len(nodes))
                ]
                key, run = make_source_ref_key(uuid4(), uuid4()), str(uuid4())
                writers.append((instance, nodes, edges, key, run))
            plans.append((instances[0], shared, writers))

        await asyncio.gather(
            *(
                _writer(instance, nodes, edges, key, run)
                for _adapter, _shared, writers in plans
                for instance, nodes, edges, key, run in writers
            )
        )

        for adapter, shared, writers in plans:
            expected_nodes = SHARED + WRITERS * (NODES_PER_WRITER - SHARED)
            nodes, edges = await adapter.get_graph_data()
            assert len(nodes) == expected_nodes
            assert len(edges) == len({(s, t, r) for w in writers for s, t, r, _ in w[2]})

            keys = {w[3] for w in writers}
            snaps = await adapter.get_node_delete_data([str(node.id) for node in shared])
            assert len(snaps) == SHARED
            for snap in snaps.values():
                assert set(snap.source_ref_keys) == keys  # both writers' refs survived the race
                assert len(snap.source_run_refs) == WRITERS
            for _instance, _nodes, _edges, key, _run in writers:
                assert len(await adapter.find_nodes_by_source_ref(key)) == NODES_PER_WRITER
    finally:
        for instances, name in zip(adapters, names, strict=True):
            for instance in instances:
                await instance.close()
            await drop_database(name)
