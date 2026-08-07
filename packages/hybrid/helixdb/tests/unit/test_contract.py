"""Offline cognee-1.4.1 conformance tests. No HelixDB server, no secrets."""

from cognee_community_hybrid_adapter_helixdb.helixdb_adapter import HelixDBAdapter
from contract_suite import assert_graph_contract, assert_vector_contract
from contract_suite.graph_contract import assert_registered as graph_registered
from contract_suite.vector_contract import assert_registered as vector_registered


def test_conforms_to_cognee_graph_contract():
    assert_graph_contract(HelixDBAdapter)


def test_conforms_to_cognee_vector_contract():
    # __init__ deploys the HQL schema and creates a client, so no offline
    # instantiation.
    assert_vector_contract(HelixDBAdapter, instantiate=False)


def test_register_adds_helixdb_providers():
    import cognee_community_hybrid_adapter_helixdb.register  # noqa: F401

    graph_registered("helixdb", HelixDBAdapter)
    vector_registered("helixdb", HelixDBAdapter)
