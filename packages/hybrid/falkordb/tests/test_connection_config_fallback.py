"""Unit tests for FalkorDB connection resolution.

The FalkorDB adapter is a hybrid store. cognee's graph engine constructs it with the
graph-named connection params, but the vector engine constructs it with only ``url`` +
``api_key`` (it never forwards a port or credentials). These tests verify the adapter
falls back to the graph config for any connection field the vector path omits, so a
FalkorDB on a non-default port or protected with ``requirepass`` stays reachable on the
vector path. No FalkorDB connection is required — the driver is mocked.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cognee_community_hybrid_adapter_falkor.falkor_adapter as adapter_module
from cognee_community_hybrid_adapter_falkor.falkor_adapter import FalkorDBAdapter


def _graph_config(**overrides):
    base = {
        "graph_database_url": "10.11.20.15",
        "graph_database_port": 6399,
        "graph_database_username": "default",
        "graph_database_password": "secret",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _construct(config=None, **adapter_kwargs):
    """Build the adapter with FalkorDB/config/embeddings mocked; return the FalkorDB() kwargs."""
    with (
        patch.object(adapter_module, "FalkorDB") as MockFalkorDB,
        patch.object(
            adapter_module, "get_graph_config", return_value=_graph_config(**(config or {}))
        ),
        patch.object(adapter_module, "get_embedding_engine", return_value=MagicMock()),
    ):
        FalkorDBAdapter(**adapter_kwargs)
        assert MockFalkorDB.call_count == 1
        return MockFalkorDB.call_args.kwargs


def test_vector_path_falls_back_to_graph_config():
    # cognee's vector engine constructs the adapter with only url + api_key.
    conn = _construct(url="10.11.20.15", api_key="k")
    assert conn["host"] == "10.11.20.15"
    assert conn["port"] == 6399
    assert conn["username"] == "default"
    assert conn["password"] == "secret"


def test_explicit_graph_params_win_over_config():
    # cognee's graph engine passes the graph-named params explicitly; config must not override them.
    conn = _construct(
        graph_database_url="graph-host",
        graph_database_port=6400,
        graph_database_username="u",
        graph_database_password="p",
    )
    assert conn["host"] == "graph-host"
    assert conn["port"] == 6400
    assert conn["username"] == "u"
    assert conn["password"] == "p"


def test_port_defaults_to_6379_when_unset_everywhere():
    conn = _construct(url="h", api_key="k", config={"graph_database_port": 0})
    assert conn["port"] == 6379


def test_add_nodes_accepts_provenance_kwargs():
    # cognee 1.x's write path (add_data_points) calls add_nodes/add_edges with
    # source_ref_key + pipeline_run_id; the adapter must accept them or it raises
    # "unexpected keyword argument" mid-cognify.
    import inspect

    params = inspect.signature(FalkorDBAdapter.add_nodes).parameters
    assert "source_ref_key" in params
    assert "pipeline_run_id" in params


def test_add_edges_accepts_provenance_kwargs():
    import inspect

    params = inspect.signature(FalkorDBAdapter.add_edges).parameters
    assert "source_ref_key" in params
    assert "pipeline_run_id" in params
