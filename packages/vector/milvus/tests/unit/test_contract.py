"""Offline cognee-1.4.1 conformance tests. No Milvus server, no secrets."""

from cognee_community_vector_adapter_milvus import MilvusAdapter
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(MilvusAdapter)


def test_register_adds_milvus_provider():
    import cognee_community_vector_adapter_milvus.register  # noqa: F401

    assert_registered("milvus", MilvusAdapter)
