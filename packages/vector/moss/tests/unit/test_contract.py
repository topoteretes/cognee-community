"""Offline cognee-1.4.1 conformance tests. No Moss account, no secrets."""

from cognee_community_vector_adapter_moss.moss_adapter import MossAdapter
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(MossAdapter)


def test_register_adds_moss_provider():
    from cognee_community_vector_adapter_moss import register  # noqa: F401

    assert_registered("moss", MossAdapter)
