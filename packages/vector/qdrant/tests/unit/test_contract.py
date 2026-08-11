"""Offline cognee-1.4.1 conformance tests. No Qdrant server, no secrets."""

from cognee_community_vector_adapter_qdrant.qdrant_adapter import QDrantAdapter
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(QDrantAdapter)


def test_register_adds_qdrant_provider():
    from cognee_community_vector_adapter_qdrant import register  # noqa: F401

    assert_registered("qdrant", QDrantAdapter)
