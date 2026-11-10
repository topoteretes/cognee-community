"""Offline tests for connection handling. No Elasticsearch server.

cognee >= 1.5.0 hands every registered vector adapter vector_db_host /
vector_db_port / vector_db_username / vector_db_password on top of url and
api_key. These tests pin what this adapter does with them and with the
Elasticsearch-specific settings (cloud id, index prefix, TLS).
"""

import pytest
from cognee_community_vector_adapter_elasticsearch import elasticsearch_adapter
from cognee_community_vector_adapter_elasticsearch.elasticsearch_adapter import (
    ElasticsearchAdapter,
)
from contract_suite import FakeEmbeddingEngine


class RecordingClient:
    """Captures the keyword arguments the adapter builds AsyncElasticsearch with."""

    last_kwargs = None

    def __init__(self, **kwargs):
        RecordingClient.last_kwargs = kwargs


@pytest.fixture(autouse=True)
def recording_client(monkeypatch):
    monkeypatch.setattr(elasticsearch_adapter, "AsyncElasticsearch", RecordingClient)
    for name in (
        "ELASTICSEARCH_CLOUD_ID",
        "ELASTICSEARCH_INDEX_PREFIX",
        "ELASTICSEARCH_VERIFY_CERTS",
        "ELASTICSEARCH_CA_CERTS",
    ):
        monkeypatch.delenv(name, raising=False)
    RecordingClient.last_kwargs = None
    return RecordingClient


def _adapter(**overrides):
    kwargs = {"url": None, "api_key": None, "embedding_engine": FakeEmbeddingEngine()}
    kwargs.update(overrides)
    return ElasticsearchAdapter(**kwargs)


def test_url_is_built_from_host_and_port_when_no_url_is_configured():
    adapter = _adapter(vector_db_host="es.internal", vector_db_port="9201")

    assert adapter.url == "http://es.internal:9201"
    assert RecordingClient.last_kwargs["hosts"] == ["http://es.internal:9201"]


def test_host_without_port_falls_back_to_the_elasticsearch_default_port():
    assert _adapter(vector_db_host="es.internal").url == "http://es.internal:9200"


def test_configured_url_wins_over_host_and_port():
    adapter = _adapter(url="https://explicit:9200", vector_db_host="ignored")

    assert adapter.url == "https://explicit:9200"


def test_nothing_configured_targets_localhost():
    assert _adapter().url == "http://localhost:9200"


def test_comma_separated_urls_become_several_hosts():
    adapter = _adapter(url="http://a:9200, http://b:9200")

    assert adapter.hosts == ["http://a:9200", "http://b:9200"]
    assert RecordingClient.last_kwargs["hosts"] == ["http://a:9200", "http://b:9200"]


def test_username_and_password_become_basic_auth():
    _adapter(vector_db_username="elastic", vector_db_password="changeme")

    assert RecordingClient.last_kwargs["basic_auth"] == ("elastic", "changeme")
    assert "api_key" not in RecordingClient.last_kwargs


def test_encoded_api_key_is_passed_through():
    _adapter(api_key="VnVhQ2ZHY0JDZGJrUW0tZTVhT3g6dWkybHAyYXhUTm1zeWFrdzl0dk5udw==")

    assert (
        RecordingClient.last_kwargs["api_key"]
        == "VnVhQ2ZHY0JDZGJrUW0tZTVhT3g6dWkybHAyYXhUTm1zeWFrdzl0dk5udw=="
    )


def test_id_colon_key_api_key_becomes_a_pair():
    _adapter(api_key="key-id:key-secret")

    assert RecordingClient.last_kwargs["api_key"] == ("key-id", "key-secret")


def test_api_key_wins_over_basic_auth():
    # The client refuses both at once, so the adapter must pick one.
    _adapter(api_key="key", vector_db_username="elastic", vector_db_password="changeme")

    assert RecordingClient.last_kwargs["api_key"] == "key"
    assert "basic_auth" not in RecordingClient.last_kwargs


def test_cloud_id_replaces_hosts(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH_CLOUD_ID", "deployment:abc")
    adapter = _adapter(api_key="key")

    assert adapter.url is None
    assert RecordingClient.last_kwargs["cloud_id"] == "deployment:abc"
    assert "hosts" not in RecordingClient.last_kwargs


def test_tls_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH_VERIFY_CERTS", "false")
    monkeypatch.setenv("ELASTICSEARCH_CA_CERTS", "/certs/http_ca.crt")
    _adapter(url="https://es:9200")

    assert RecordingClient.last_kwargs["verify_certs"] is False
    assert RecordingClient.last_kwargs["ca_certs"] == "/certs/http_ca.crt"


def test_tls_settings_are_omitted_unless_configured():
    _adapter(url="https://es:9200")

    assert "verify_certs" not in RecordingClient.last_kwargs
    assert "ca_certs" not in RecordingClient.last_kwargs


def test_index_names_are_prefixed_and_lowercased():
    adapter = _adapter()

    assert adapter.index_prefix == "cognee"
    assert adapter._index("DocumentChunk_text") == "cognee-documentchunk_text"


def test_index_prefix_from_environment_is_sanitized(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH_INDEX_PREFIX", "_My App:v2")

    assert _adapter()._index("Entity_name") == "my-app-v2-entity_name"


def test_explicit_index_prefix_wins_over_environment(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH_INDEX_PREFIX", "env")

    assert _adapter(index_prefix="arg").index_prefix == "arg"


def test_empty_index_prefix_is_refused():
    with pytest.raises(ValueError, match="index prefix"):
        _adapter(index_prefix="---")


def test_document_ids_are_scoped_per_dataset():
    a = _adapter(database_name="dataset-a")
    b = _adapter(database_name="dataset-b")
    point_id = "6f7d3a2e-5c1b-4f0e-9a8d-1b2c3d4e5f60"

    assert a._doc_id(point_id) == a._doc_id(point_id)
    assert a._doc_id(point_id) != b._doc_id(point_id)
    assert a._doc_id(point_id) != point_id


def test_injected_client_is_used_as_is():
    client = object()
    adapter = _adapter(client=client)

    assert adapter.client is client
    assert RecordingClient.last_kwargs is None
