"""Offline cognee-1.4.1 conformance tests. No SingleStore server, no secrets."""

from cognee_community_vector_adapter_singlestore.singlestore_adapter import (
    SingleStoreAdapter,
)
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(SingleStoreAdapter, instantiate=False)


def test_register_adds_singlestore_provider():
    from cognee_community_vector_adapter_singlestore import register  # noqa: F401

    assert_registered("singlestore", SingleStoreAdapter)
