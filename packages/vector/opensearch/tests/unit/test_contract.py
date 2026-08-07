"""Offline cognee-1.4.1 conformance tests. No OpenSearch server, no secrets."""

from cognee_community_vector_adapter_opensearch.opensearch_adapter import (
    OpenSearchAdapter,
)
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(OpenSearchAdapter, instantiate=False)


def test_register_adds_opensearch_provider():
    import cognee_community_vector_adapter_opensearch.register  # noqa: F401

    assert_registered("opensearch", OpenSearchAdapter)
