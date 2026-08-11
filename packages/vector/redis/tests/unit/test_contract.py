"""Offline cognee-1.4.1 conformance tests. No Redis server, no secrets."""

from cognee.infrastructure.databases.vector.exceptions import (
    CollectionNotFoundError as CogneeCollectionNotFoundError,
)
from cognee_community_vector_adapter_redis.redis_adapter import (
    CollectionNotFoundError,
    RedisAdapter,
)
from contract_suite import assert_vector_contract
from contract_suite.vector_contract import assert_registered


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(RedisAdapter)


def test_register_adds_redis_provider():
    import cognee_community_vector_adapter_redis.register  # noqa: F401

    assert_registered("redis", RedisAdapter)


def test_collection_not_found_is_catchable_by_cognee_core():
    # cognee's retrieval code catches its own CollectionNotFoundError to treat
    # missing collections as empty results; the adapter's local class must
    # therefore subclass it.
    assert issubclass(CollectionNotFoundError, CogneeCollectionNotFoundError)
