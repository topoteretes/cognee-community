"""Offline tests for the cognee 1.5.x connection keywords. No Milvus server.

cognee >= 1.5.0 hands every registered vector adapter vector_db_host /
vector_db_port / vector_db_username / vector_db_password on top of url and
api_key. These tests pin what this adapter does with them.
"""

from cognee_community_vector_adapter_milvus.milvus_adapter import MilvusAdapter
from contract_suite import FakeEmbeddingEngine


def _adapter(**overrides):
    kwargs = {
        "url": "",
        "api_key": None,
        "embedding_engine": FakeEmbeddingEngine(),
    }
    kwargs.update(overrides)
    return MilvusAdapter(**kwargs)


def test_uri_is_built_from_host_and_port_when_no_url_is_configured():
    adapter = _adapter(vector_db_host="milvus.internal", vector_db_port="19531")

    assert adapter.url == "http://milvus.internal:19531"


def test_host_without_port_falls_back_to_the_milvus_default_port():
    adapter = _adapter(vector_db_host="milvus.internal")

    assert adapter.url == "http://milvus.internal:19530"


def test_configured_url_wins_over_host_and_port():
    adapter = _adapter(url="http://explicit:19530", vector_db_host="ignored")

    assert adapter.url == "http://explicit:19530"


def test_username_and_password_become_the_milvus_token():
    adapter = _adapter(vector_db_username="user", vector_db_password="password")

    assert adapter.api_key == "user:password"


def test_api_key_wins_over_username_and_password():
    adapter = _adapter(api_key="token", vector_db_username="user", vector_db_password="password")

    assert adapter.api_key == "token"


def test_half_a_credential_pair_is_not_turned_into_a_token():
    adapter = _adapter(vector_db_username="user")

    assert adapter.api_key is None
