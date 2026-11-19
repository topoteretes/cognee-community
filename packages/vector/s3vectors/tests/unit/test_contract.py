"""Offline cognee-1.6.1 conformance tests. No AWS account, no secrets."""

from cognee_community_vector_adapter_s3vectors.s3vectors_adapter import S3VectorsAdapter
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    # boto3 resolves credentials and the endpoint lazily, so the dummy bucket
    # and endpoint in the factory kwargs keep construction fully offline.
    assert_vector_contract(
        S3VectorsAdapter,
        constructor_kwargs={"vector_bucket_name": "contract-test-bucket"},
    )


def test_register_adds_s3vectors_provider():
    import cognee_community_vector_adapter_s3vectors.register  # noqa: F401

    assert_registered("s3vectors", S3VectorsAdapter)


def test_register_adds_s3vectors_dataset_handler():
    import cognee_community_vector_adapter_s3vectors.register  # noqa: F401
    from cognee.infrastructure.databases.dataset_database_handler import (
        supported_dataset_database_handlers,
    )
    from cognee_community_vector_adapter_s3vectors.S3VectorsDatasetDatabaseHandler import (
        S3VectorsDatasetDatabaseHandler,
    )

    entry = supported_dataset_database_handlers["s3vectors"]
    assert entry["handler_instance"] is S3VectorsDatasetDatabaseHandler
    assert entry["handler_provider"] == "s3vectors"
