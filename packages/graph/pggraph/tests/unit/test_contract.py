"""Offline cognee-1.4.1 conformance tests. No database, no secrets."""

from cognee_community_graph_adapter_pggraph import PgGraphAdapter, register
from contract_suite import assert_graph_contract
from contract_suite.graph_contract import assert_registered


def test_conforms_to_cognee_graph_contract():
    assert_graph_contract(PgGraphAdapter)


def test_register_adds_pggraph_provider():
    register()
    assert_registered("pggraph", PgGraphAdapter)
