"""Offline cognee-1.4.1 conformance tests. No Weaviate cluster, no secrets."""

from cognee_community_vector_adapter_weaviate.weaviate_adapter import WeaviateAdapter
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(WeaviateAdapter)


def test_register_adds_weaviate_provider():
    import cognee_community_vector_adapter_weaviate.register  # noqa: F401

    assert_registered("weaviate", WeaviateAdapter)
