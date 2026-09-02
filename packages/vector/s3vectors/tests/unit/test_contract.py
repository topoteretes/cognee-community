"""Offline cognee-1.4.1 conformance tests. No AWS account, no secrets."""

from cognee_community_vector_adapter_s3vectors.s3vectors_adapter import (
    S3VectorsAdapter,
)
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(
        S3VectorsAdapter,
        constructor_kwargs={"database_name": "contract-test-bucket"},
    )


def test_register_adds_s3vectors_provider():
    import cognee_community_vector_adapter_s3vectors.register  # noqa: F401

    assert_registered("s3vectors", S3VectorsAdapter)
