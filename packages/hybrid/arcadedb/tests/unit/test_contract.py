"""Offline cognee-1.4.1 conformance tests. No ArcadeDB server, no secrets."""

from cognee_community_hybrid_adapter_arcadedb.arcadedb_adapter import (
    ArcadeDBAdapter,
    ArcadeDBVectorAdapter,
)
from contract_suite import assert_graph_contract, assert_vector_contract
from contract_suite.graph_contract import assert_registered as graph_registered
from contract_suite.vector_contract import assert_registered as vector_registered


def test_conforms_to_cognee_graph_contract():
    assert_graph_contract(ArcadeDBAdapter)


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(ArcadeDBVectorAdapter, instantiate=False)


def test_register_adds_arcadedb_providers():
    import cognee_community_hybrid_adapter_arcadedb.register  # noqa: F401

    graph_registered("arcadedb", ArcadeDBAdapter)
    vector_registered("arcadedb", ArcadeDBVectorAdapter)
