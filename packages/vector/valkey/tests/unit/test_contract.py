"""Offline cognee-1.4.1 conformance tests. No Valkey server, no secrets."""

from cognee.infrastructure.databases.vector.exceptions import (
    CollectionNotFoundError as CogneeCollectionNotFoundError,
)
from cognee_community_vector_adapter_valkey.exceptions import CollectionNotFoundError
from cognee_community_vector_adapter_valkey.valkey_adapter import ValkeyAdapter
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(ValkeyAdapter)


def test_register_adds_valkey_provider():
    import cognee_community_vector_adapter_valkey.register  # noqa: F401

    assert_registered("valkey", ValkeyAdapter)


def test_collection_not_found_is_catchable_by_cognee_core():
    assert issubclass(CollectionNotFoundError, CogneeCollectionNotFoundError)
