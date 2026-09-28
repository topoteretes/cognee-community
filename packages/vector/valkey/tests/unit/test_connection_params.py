"""Offline tests for the cognee 1.5.x connection keywords. No Valkey server.

cognee >= 1.5.0 hands every registered vector adapter vector_db_host /
vector_db_port / vector_db_username / vector_db_password on top of url and
api_key. These tests pin what this adapter does with them.
"""

from cognee_community_vector_adapter_valkey.valkey_adapter import ValkeyAdapter
from contract_suite import FakeEmbeddingEngine


def _adapter(**overrides):
    kwargs = {
        "url": "",
        "embedding_engine": FakeEmbeddingEngine(),
    }
    kwargs.update(overrides)
    return ValkeyAdapter(**kwargs)


def test_host_and_port_are_used_when_no_url_is_configured():
    adapter = _adapter(vector_db_host="valkey.internal", vector_db_port="6380")

    assert (adapter._host, adapter._port) == ("valkey.internal", 6380)


def test_host_without_port_falls_back_to_the_valkey_default_port():
    adapter = _adapter(vector_db_host="valkey.internal")

    assert (adapter._host, adapter._port) == ("valkey.internal", 6379)


def test_configured_url_wins_over_host_and_port():
    adapter = _adapter(url="valkey://explicit:6381", vector_db_host="ignored")

    assert (adapter._host, adapter._port) == ("explicit", 6381)


def test_no_url_and_no_host_keeps_the_previous_localhost_default():
    adapter = _adapter()

    assert (adapter._host, adapter._port) == ("localhost", 6379)


def test_unused_credentials_are_absorbed():
    # This adapter connects without auth; the username/password pair cognee
    # always passes must not reach the constructor as an unexpected keyword.
    adapter = _adapter(
        vector_db_host="valkey.internal",
        vector_db_username="user",
        vector_db_password="password",
    )

    assert adapter._host == "valkey.internal"
