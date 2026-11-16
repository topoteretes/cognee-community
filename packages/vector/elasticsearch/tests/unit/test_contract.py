"""Offline cognee-1.6.7 conformance tests. No Elasticsearch server, no secrets."""

from cognee_community_vector_adapter_elasticsearch.elasticsearch_adapter import (
    ElasticsearchAdapter,
)
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    # The client connects lazily, so construction with dummy connection info is safe.
    assert_vector_contract(ElasticsearchAdapter, instantiate=True)


def test_register_adds_elasticsearch_provider_and_dataset_handler():
    import cognee_community_vector_adapter_elasticsearch.register  # noqa: F401
    from cognee.infrastructure.databases.dataset_database_handler import (
        supported_dataset_database_handlers,
    )
    from cognee_community_vector_adapter_elasticsearch import (
        ElasticsearchDatasetDatabaseHandler,
    )

    assert_registered("elasticsearch", ElasticsearchAdapter)
    handler = supported_dataset_database_handlers["elasticsearch"]
    assert handler["handler_instance"] is ElasticsearchDatasetDatabaseHandler
    assert handler["handler_provider"] == "elasticsearch"
