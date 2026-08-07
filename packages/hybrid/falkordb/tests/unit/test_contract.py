"""Offline cognee-1.4.1 conformance tests. No FalkorDB server, no secrets."""

from cognee_community_hybrid_adapter_falkor.falkor_adapter import FalkorDBAdapter
from contract_suite import assert_graph_contract, assert_vector_contract
from contract_suite.graph_contract import assert_registered as graph_registered
from contract_suite.vector_contract import assert_registered as vector_registered


def test_conforms_to_cognee_graph_contract():
    assert_graph_contract(FalkorDBAdapter)


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(FalkorDBAdapter, instantiate=False)


def test_register_adds_falkor_providers():
    import cognee_community_hybrid_adapter_falkor.register  # noqa: F401

    graph_registered("falkor", FalkorDBAdapter)
    vector_registered("falkor", FalkorDBAdapter)
