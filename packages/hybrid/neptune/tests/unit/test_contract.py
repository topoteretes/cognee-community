"""Offline cognee conformance tests. No Neptune graph, no AWS credentials, no secrets."""

from cognee_community_hybrid_adapter_neptune import NeptuneAnalyticsAdapter, NeptuneGraphDB
from contract_suite import assert_graph_contract, assert_vector_contract
from contract_suite.graph_contract import assert_registered as graph_registered
from contract_suite.vector_contract import assert_registered as vector_registered


def test_graph_adapter_conforms_to_cognee_graph_contract():
    assert_graph_contract(NeptuneGraphDB)


def test_hybrid_adapter_conforms_to_cognee_graph_contract():
    assert_graph_contract(NeptuneAnalyticsAdapter)


def test_hybrid_adapter_conforms_to_cognee_vector_contract():
    # __init__ builds a boto client for the graph, so it is not instantiated here.
    assert_vector_contract(NeptuneAnalyticsAdapter, instantiate=False)


def test_register_adds_neptune_providers():
    import cognee_community_hybrid_adapter_neptune.register  # noqa: F401

    graph_registered("neptune", NeptuneGraphDB)
    graph_registered("neptune_analytics", NeptuneAnalyticsAdapter)
    vector_registered("neptune_analytics", NeptuneAnalyticsAdapter)
