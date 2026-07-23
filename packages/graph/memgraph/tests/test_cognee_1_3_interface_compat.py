"""cognee 1.3.0 interface-compatibility tests for MemgraphAdapter.

cognee's non-hybrid write path calls ``add_nodes`` / ``add_edges`` with the
``source_ref_key`` and ``pipeline_run_id`` provenance keyword arguments
(``cognee/tasks/storage/add_data_points.py``). Memgraph is a graph-only adapter,
so it takes that branch; without these parameters cognify raises
``TypeError: add_nodes() got an unexpected keyword argument 'source_ref_key'``.

These are pure signature checks — no running Memgraph server required.
"""

import inspect

from cognee_community_graph_adapter_memgraph.memgraph_adapter import MemgraphAdapter

PROVENANCE_KWARGS = ("source_ref_key", "pipeline_run_id")


def test_add_nodes_accepts_provenance_kwargs():
    params = inspect.signature(MemgraphAdapter.add_nodes).parameters
    for kw in PROVENANCE_KWARGS:
        assert kw in params, f"add_nodes must accept '{kw}' (cognee 1.3.0 non-hybrid path)"
        assert params[kw].default is None, f"add_nodes '{kw}' must default to None"


def test_add_edges_accepts_provenance_kwargs():
    params = inspect.signature(MemgraphAdapter.add_edges).parameters
    for kw in PROVENANCE_KWARGS:
        assert kw in params, f"add_edges must accept '{kw}' (cognee 1.3.0 non-hybrid path)"
        assert params[kw].default is None, f"add_edges '{kw}' must default to None"
