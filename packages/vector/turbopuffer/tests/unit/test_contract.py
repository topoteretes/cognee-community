"""Offline cognee-1.4.1 conformance tests. No TurboPuffer account, no secrets."""

from cognee_community_vector_adapter_turbopuffer.turbopuffer_adapter import (
    TurbopufferAdapter,
)
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(TurbopufferAdapter)


def test_register_adds_turbopuffer_provider():
    from cognee_community_vector_adapter_turbopuffer import register  # noqa: F401

    assert_registered("turbopuffer", TurbopufferAdapter)
