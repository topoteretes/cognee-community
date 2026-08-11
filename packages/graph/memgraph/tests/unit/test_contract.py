"""Offline cognee-1.4.1 conformance tests. No database, no secrets."""

from contract_suite import assert_graph_contract
from contract_suite.graph_contract import assert_registered

from cognee_community_graph_adapter_memgraph import register
from cognee_community_graph_adapter_memgraph.memgraph_adapter import MemgraphAdapter


def test_conforms_to_cognee_graph_contract():
    assert_graph_contract(MemgraphAdapter)


def test_register_adds_memgraph_provider():
    register()
    assert_registered("memgraph", MemgraphAdapter)
