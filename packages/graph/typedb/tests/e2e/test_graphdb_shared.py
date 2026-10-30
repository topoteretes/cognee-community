"""End-to-end tier: cognee's shared graph-backend test against TypeDB.

Runs cognee's own ``run_graph_db_test`` (add two documents -> cognify ->
GRAPH_COMPLETION / CHUNKS / SUMMARIES -> NodeSet filter -> prune) with the
TypeDB provider in BOTH access-control modes, plus dataset deletion through
the per-dataset handler. Spends LLM credits, so it is opt-in:
``RUN_E2E_TESTS=1`` plus a TypeDB server and an LLM key. Writes go to a
dedicated ``cognee_e2e`` database (or per-dataset databases), never to the
default ``cognee`` one.
"""

import os

import cognee
import pytest
from cognee.tests.e2e.postgres.test_graphdb_shared import run_graph_db_test
from support import (
    ADDRESS,
    E2E_DATABASE,
    database_exists,
    graph_db_config,
    server_available,
)

pytestmark = [
    pytest.mark.skipif(os.environ.get("RUN_E2E_TESTS") != "1", reason="set RUN_E2E_TESTS=1"),
    pytest.mark.skipif(not server_available(), reason=f"no TypeDB server at {ADDRESS}"),
    pytest.mark.skipif(not os.environ.get("LLM_API_KEY"), reason="LLM_API_KEY not set"),
]


@pytest.fixture
def e2e_config(typedb_config):
    cognee.config.set_graph_db_config(graph_db_config(graph_database_name=E2E_DATABASE))


@pytest.mark.parametrize("access_control", ["true", "false"])
async def test_shared_graph_db_suite(e2e_config, monkeypatch, access_control):
    # cognee reads this at call time, so it beats whatever .env loaded.
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", access_control)
    await run_graph_db_test("typedb")


async def test_delete_dataset_drops_its_typedb_database(e2e_config, monkeypatch):
    from cognee.context_global_variables import backend_access_control_enabled
    from cognee.modules.data.methods import delete_dataset, get_datasets_by_name
    from cognee.modules.users.methods import get_default_user

    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "true")
    assert backend_access_control_enabled()

    dataset_name = "typedb_delete_me"
    await cognee.add(["TypeDB deletes the whole dataset database on request."], dataset_name)
    await cognee.cognify([dataset_name])

    user = await get_default_user()
    dataset = (await get_datasets_by_name([dataset_name], user.id))[0]
    database_name = f"cognee_{dataset.id.hex}"

    assert await database_exists(database_name)
    await delete_dataset(dataset)
    assert not await database_exists(database_name)


async def test_graph_native_delete_removes_exclusive_nodes(e2e_config, isolated_roots, monkeypatch):
    """Cognee marks a fresh TypeDB graph as provenance-backed and deletes one
    document's exclusive nodes through the graph, not the relational ledger
    (mirrors cognee's ``test_delete_default_graph_non_mocked``)."""
    from cognee.api.v1.datasets import datasets
    from cognee.infrastructure.databases.graph import get_graph_engine
    from cognee.infrastructure.databases.provenance import make_source_ref_key
    from cognee.infrastructure.databases.provenance.markers import stores_provenance_in_graph
    from cognee.modules.users.methods import get_default_user

    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "false")
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    john = await cognee.add(
        "John works for Apple. He is also affiliated with a non-profit "
        "organization called 'Food for Hungry'."
    )
    marie = await cognee.add("Marie works for Apple as well. She is a software engineer.")
    johns_data_id = john.data_ingestion_info[0]["data_id"]
    maries_data_id = marie.data_ingestion_info[0]["data_id"]

    cognify_result = await cognee.cognify()
    dataset_id = next(iter(cognify_result))

    graph_engine = await get_graph_engine()
    assert await stores_provenance_in_graph(graph_engine)

    async def exclusive_nodes(source_ref_key):
        node_ids = await graph_engine.find_nodes_by_source_ref(source_ref_key)
        node_data = await graph_engine.get_node_delete_data(node_ids)
        return {
            node_id
            for node_id, data in node_data.items()
            if set(data.source_ref_keys) == {source_ref_key}
        }

    john_nodes = await exclusive_nodes(make_source_ref_key(dataset_id, johns_data_id))
    marie_nodes = await exclusive_nodes(make_source_ref_key(dataset_id, maries_data_id))
    assert john_nodes and marie_nodes

    user = await get_default_user()
    await datasets.delete_data(dataset_id, johns_data_id, user)

    assert await graph_engine.get_nodes(list(john_nodes)) == []
    assert len(await graph_engine.get_nodes(list(marie_nodes))) == len(marie_nodes)
    nodes, edges = await graph_engine.get_graph_data()
    assert not any(src in john_nodes or tgt in john_nodes for src, tgt, _, _ in edges)
    assert not any(node[1].get("name", "").lower() in {"john", "food for hungry"} for node in nodes)

    await datasets.delete_data(dataset_id, maries_data_id, user, delete_dataset_if_empty=True)
    final_nodes, final_edges = await graph_engine.get_graph_data()
    assert (final_nodes, final_edges) == ([], [])
