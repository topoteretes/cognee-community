"""Offline cognee-1.4.1 conformance tests. No services, no secrets."""

from cognee.infrastructure.databases.vector.exceptions import (
    CollectionNotFoundError as CogneeCollectionNotFoundError,
)
from cognee_community_hybrid_adapter_duckdb.duckdb_adapter import (
    CollectionNotFoundError,
    DuckDBAdapter,
)
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    # DuckDB registers as a vector adapter only; its graph-side methods are
    # explicit NotImplementedError stubs.
    assert_vector_contract(DuckDBAdapter, instantiate=False)


def test_register_adds_duckdb_provider():
    import cognee_community_hybrid_adapter_duckdb.register  # noqa: F401

    assert_registered("duckdb", DuckDBAdapter)


def test_collection_not_found_is_catchable_by_cognee_core():
    assert issubclass(CollectionNotFoundError, CogneeCollectionNotFoundError)
