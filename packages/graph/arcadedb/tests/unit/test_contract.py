"""Offline cognee-1.4.1 conformance tests. No database, no secrets."""

from contract_suite import assert_graph_contract
from contract_suite.graph_contract import assert_registered

from cognee_community_graph_adapter_arcadedb import register
from cognee_community_graph_adapter_arcadedb.arcadedb_adapter import ArcadeDBAdapter


def test_conforms_to_cognee_graph_contract():
    assert_graph_contract(ArcadeDBAdapter)


def test_register_adds_arcadedb_provider():
    register()
    assert_registered("arcadedb", ArcadeDBAdapter)
