"""Offline cognee conformance tests. No server anywhere: Infino is embedded,
so even the behavioral tier below runs against the real engine with no
network, no secrets."""

from cognee_community_vector_adapter_infino.infino_adapter import InfinoAdapter
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(InfinoAdapter)


def test_register_adds_infino_provider():
    from cognee_community_vector_adapter_infino import register  # noqa: F401

    assert_registered("infino", InfinoAdapter)
