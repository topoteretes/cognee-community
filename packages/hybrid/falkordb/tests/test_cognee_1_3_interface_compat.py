"""Unit tests for cognee 1.3.0 interface-compatibility fixes (issue #142).

Covers three version-skew gaps in ``FalkorDBAdapter``, all with a mocked
FalkorDB driver — no live database required:

1. ``add_nodes`` / ``add_edges`` accept the ``source_ref_key`` /
   ``pipeline_run_id`` provenance kwargs cognee 1.3.0 passes.
2. The vector-engine construction path honours a ``host:port`` URL instead of
   always dialing ``:6379``.
3. ``remove_belongs_to_set_tags`` is overridden (no longer a base-class no-op).
"""

import asyncio
from unittest.mock import MagicMock, patch

from cognee_community_hybrid_adapter_falkor.falkor_adapter import FalkorDBAdapter


def _make_adapter_capturing_queries():
    """Adapter with a mocked driver that records (query, params) calls."""
    adapter = object.__new__(FalkorDBAdapter)
    adapter.graph_name = "test_graph"

    mock_result = MagicMock()
    mock_result.result_set = []

    mock_graph = MagicMock()
    mock_graph.query.return_value = mock_result

    mock_driver = MagicMock()
    mock_driver.select_graph.return_value = mock_graph

    adapter.driver = mock_driver
    return adapter, mock_graph


# --- Defect 1: provenance kwargs accepted ------------------------------------


def test_add_nodes_accepts_provenance_kwargs():
    """add_nodes must not raise TypeError on cognee 1.3.0's provenance kwargs."""
    adapter, _ = _make_adapter_capturing_queries()
    # Empty list short-circuits; the point is that the call binds without a
    # ``unexpected keyword argument`` TypeError.
    asyncio.run(adapter.add_nodes([], source_ref_key="ref", pipeline_run_id="run"))


def test_add_edges_accepts_provenance_kwargs():
    """add_edges must not raise TypeError on cognee 1.3.0's provenance kwargs."""
    adapter, _ = _make_adapter_capturing_queries()
    asyncio.run(adapter.add_edges([], source_ref_key="ref", pipeline_run_id="run"))


# --- Defect 2: host:port parsing ---------------------------------------------


def test_split_host_port_parses_bare_host_port():
    assert FalkorDBAdapter._split_host_port("myhost:6380", 6379) == ("myhost", 6380)


def test_split_host_port_bare_host_keeps_default():
    assert FalkorDBAdapter._split_host_port("localhost", 6379) == ("localhost", 6379)


def test_split_host_port_ignores_scheme_url():
    # A scheme'd URL is left untouched (default port).
    assert FalkorDBAdapter._split_host_port("redis://h:6380", 6379) == (
        "redis://h:6380",
        6379,
    )


def test_split_host_port_ignores_ipv6_literal():
    # Multiple colons => not a bare host:port; leave untouched.
    assert FalkorDBAdapter._split_host_port("::1", 6379) == ("::1", 6379)


def test_split_host_port_non_string_passthrough():
    assert FalkorDBAdapter._split_host_port(None, 6379) == (None, 6379)


def test_init_honours_port_from_url():
    """The vector path passes only url=...; a :port in it must reach FalkorDB."""
    with (
        patch("cognee_community_hybrid_adapter_falkor.falkor_adapter.FalkorDB") as mock_falkor,
        patch(
            "cognee_community_hybrid_adapter_falkor.falkor_adapter.get_embedding_engine"
        ) as mock_embed,
    ):
        mock_embed.return_value = MagicMock()
        FalkorDBAdapter(url="myhost:6380")

    _, kwargs = mock_falkor.call_args
    assert kwargs["host"] == "myhost"
    assert kwargs["port"] == 6380


def test_init_bare_host_uses_default_port():
    with (
        patch("cognee_community_hybrid_adapter_falkor.falkor_adapter.FalkorDB") as mock_falkor,
        patch(
            "cognee_community_hybrid_adapter_falkor.falkor_adapter.get_embedding_engine"
        ) as mock_embed,
    ):
        mock_embed.return_value = MagicMock()
        FalkorDBAdapter(url="localhost")

    _, kwargs = mock_falkor.call_args
    assert kwargs["host"] == "localhost"
    assert kwargs["port"] == 6379


# --- Defect 3: remove_belongs_to_set_tags ------------------------------------


def test_remove_belongs_to_set_tags_empty_is_noop():
    """No tags => no query issued."""
    adapter, mock_graph = _make_adapter_capturing_queries()
    asyncio.run(adapter.remove_belongs_to_set_tags([]))
    mock_graph.query.assert_not_called()


def test_remove_belongs_to_set_tags_issues_strip_and_delete():
    adapter, mock_graph = _make_adapter_capturing_queries()
    asyncio.run(adapter.remove_belongs_to_set_tags(["Dev"]))

    mock_graph.query.assert_called_once()
    query, params = mock_graph.query.call_args[0]
    assert "belongs_to_set" in query
    assert "DETACH DELETE" in query
    assert params["tags"] == ["Dev"]
    assert "node_ids" not in params


def test_remove_belongs_to_set_tags_scoped_by_node_ids():
    adapter, mock_graph = _make_adapter_capturing_queries()
    asyncio.run(adapter.remove_belongs_to_set_tags(["Dev"], node_ids=["abc"]))

    query, params = mock_graph.query.call_args[0]
    assert "n.id IN $node_ids" in query
    assert params["node_ids"] == ["abc"]
