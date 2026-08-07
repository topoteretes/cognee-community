"""Offline cognee-1.4.1 conformance tests. No openGauss server, no secrets."""

from cognee_community_vector_adapter_opengauss import OpenGaussAdapter
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(OpenGaussAdapter, instantiate=False)


def test_register_adds_opengauss_provider():
    import cognee_community_vector_adapter_opengauss.register  # noqa: F401

    assert_registered("opengauss", OpenGaussAdapter)
