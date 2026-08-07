"""Offline cognee-1.4.1 conformance tests. No Azure account, no secrets."""

from cognee_community_vector_adapter_azure.azureaisearch_adapter import (
    AzureAISearchAdapter,
)
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    # Azure's SDK refuses key credentials on non-https endpoints, so the dummy
    # construction URL must be https.
    assert_vector_contract(
        AzureAISearchAdapter,
        constructor_kwargs={"url": "https://contract-test.search.windows.net"},
    )


def test_register_adds_azureaisearch_provider():
    import cognee_community_vector_adapter_azure.register  # noqa: F401

    assert_registered("azureaisearch", AzureAISearchAdapter)
