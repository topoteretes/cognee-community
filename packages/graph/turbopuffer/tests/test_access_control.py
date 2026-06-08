"""Backend access-control (multi-tenant) test for the TurboPuffer graph adapter.

Exercises the real cognee path that the dataset handler exists for: with
ENABLE_BACKEND_ACCESS_CONTROL=true and GRAPH_DATASET_DATABASE_HANDLER=turbopuffer_graph,
cognee creates a per-dataset graph database (its own namespace prefix) via
TurbopufferGraphDatasetDatabaseHandler.create_dataset, writes there during
cognify, and prunes it on cleanup.

Verifies: two datasets land in two distinct, isolated graph namespaces, each
populated, and pruning removes them. Requires TURBOPUFFER_API_KEY + LLM_API_KEY
and COGNEE_TURBOPUFFER_GRAPH_E2E=1.
"""

import os

import pytest
from conftest import requires_e2e


def _graph_namespaces(prefixes=None):
    """Return {namespace_name: row_count} for graph_node namespaces, optionally
    filtered to those whose prefix is in `prefixes`."""
    import turbopuffer

    client = turbopuffer.Turbopuffer(
        api_key=os.environ["TURBOPUFFER_API_KEY"],
        region=os.getenv("TURBOPUFFER_REGION", "gcp-us-central1"),
    )
    out = {}
    for ns in client.namespaces():
        name = getattr(ns, "id", None) or str(ns)
        if not name.endswith("_graph_node"):
            continue
        if prefixes is not None and not any(name.startswith(p) for p in prefixes):
            continue
        rows = client.namespace(name).query(rank_by=("id", "asc"), top_k=2000).rows or []
        out[name] = len(rows)
    return out


@requires_e2e
@pytest.mark.asyncio
async def test_backend_access_control_isolates_datasets():
    os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] = "true"

    import cognee
    from cognee.infrastructure.databases.graph.config import get_graph_config
    from cognee_community_graph_adapter_turbopuffer import register

    register()

    graph_config = get_graph_config()
    prev_provider = graph_config.graph_database_provider
    prev_handler = graph_config.graph_dataset_database_handler

    cognee.config.set_graph_database_provider("turbopuffer")
    graph_config.graph_dataset_database_handler = "turbopuffer_graph"

    ds_a, ds_b = "acl_alice", "acl_rabbit"
    new_ns: dict = {}
    try:
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)

        before = set(_graph_namespaces())

        await cognee.add("Alice fell down the rabbit hole into Wonderland.", ds_a)
        await cognee.add("The White Rabbit checked his pocket watch nervously.", ds_b)
        await cognee.cognify([ds_a, ds_b])

        after = _graph_namespaces()
        new_ns = {n: c for n, c in after.items() if n not in before}

        # Access control routes each dataset to its own per-dataset namespace,
        # each independently populated (isolation).
        assert len(new_ns) >= 2, f"expected >=2 per-dataset graph namespaces, got {new_ns}"
        assert all(count > 0 for count in new_ns.values()), new_ns

        # GRAPH_COMPLETION still works under access control.
        from cognee.modules.search.types import SearchType

        results = await cognee.search(query_type=SearchType.GRAPH_COMPLETION, query_text="Alice")
        assert results is not None
    finally:
        await cognee.prune.prune_data()
        await cognee.prune.prune_system(metadata=True)
        graph_config.graph_database_provider = prev_provider
        graph_config.graph_dataset_database_handler = prev_handler

    # Prune removed every per-dataset namespace this test created.
    remaining = set(_graph_namespaces())
    assert not (set(new_ns) & remaining), f"namespaces survived prune: {set(new_ns) & remaining}"
