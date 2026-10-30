"""Offline tests for the cognee 1.5.x connection keywords. No Qdrant server.

cognee >= 1.5.0 hands every registered vector adapter vector_db_host /
vector_db_port / vector_db_username / vector_db_password on top of url and
api_key. These tests pin what this adapter does with them.
"""

from cognee_community_vector_adapter_qdrant.qdrant_adapter import QDrantAdapter
from contract_suite import FakeEmbeddingEngine


def _adapter(**overrides):
    kwargs = {
        "url": None,
        "api_key": "key",
        "embedding_engine": FakeEmbeddingEngine(),
    }
    kwargs.update(overrides)
    return QDrantAdapter(**kwargs)


def test_url_is_built_from_host_and_port_when_no_url_is_configured():
    adapter = _adapter(vector_db_host="qdrant.internal", vector_db_port="7333")

    assert adapter.url == "http://qdrant.internal:7333"


def test_host_without_port_falls_back_to_the_qdrant_default_port():
    adapter = _adapter(vector_db_host="qdrant.internal")

    assert adapter.url == "http://qdrant.internal:6333"


def test_configured_url_wins_over_host_and_port():
    adapter = _adapter(url="http://explicit:6333", vector_db_host="ignored")

    assert adapter.url == "http://explicit:6333"


def test_unused_credentials_are_absorbed():
    # Qdrant authenticates with an API key; the username/password pair cognee
    # always passes must not reach the constructor as an unexpected keyword.
    adapter = _adapter(vector_db_username="user", vector_db_password="password")

    assert adapter.api_key == "key"
