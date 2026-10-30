"""Offline tests for the cognee 1.5.x connection keywords. No Redis server.

cognee >= 1.5.0 hands every registered vector adapter vector_db_host /
vector_db_port / vector_db_username / vector_db_password on top of url and
api_key. These tests pin what this adapter does with them.
"""

import pytest
from cognee_community_vector_adapter_redis.redis_adapter import (
    RedisAdapter,
    VectorEngineInitializationError,
)
from contract_suite import FakeEmbeddingEngine


def _adapter(**overrides):
    kwargs = {
        "url": "",
        "embedding_engine": FakeEmbeddingEngine(),
    }
    kwargs.update(overrides)
    return RedisAdapter(**kwargs)


def test_url_is_built_from_host_and_port_when_no_url_is_configured():
    adapter = _adapter(vector_db_host="redis.internal", vector_db_port="6380")

    assert adapter.url == "redis://redis.internal:6380"


def test_host_without_port_falls_back_to_the_redis_default_port():
    adapter = _adapter(vector_db_host="redis.internal")

    assert adapter.url == "redis://redis.internal:6379"


def test_credentials_are_embedded_in_the_built_url():
    adapter = _adapter(
        vector_db_host="redis.internal",
        vector_db_username="user",
        vector_db_password="p@ss word",
    )

    assert adapter.url == "redis://user:p%40ss%20word@redis.internal:6379"


def test_password_without_username_still_authenticates():
    adapter = _adapter(vector_db_host="redis.internal", vector_db_password="secret")

    assert adapter.url == "redis://:secret@redis.internal:6379"


def test_configured_url_wins_over_host_and_port():
    adapter = _adapter(url="redis://explicit:6379", vector_db_host="ignored")

    assert adapter.url == "redis://explicit:6379"


def test_no_url_and_no_host_still_raises():
    with pytest.raises(VectorEngineInitializationError):
        _adapter()
