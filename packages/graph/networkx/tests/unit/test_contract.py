"""Offline cognee-1.4.1 conformance tests. No services, no secrets."""

from cognee_community_graph_adapter_networkx import NetworkXAdapter
from contract_suite import assert_graph_contract
from contract_suite.graph_contract import assert_registered


def test_conforms_to_cognee_graph_contract():
    assert_graph_contract(NetworkXAdapter)


def test_register_adds_networkx_provider():
    import cognee_community_graph_adapter_networkx.register  # noqa: F401

    assert_registered("networkx", NetworkXAdapter)
