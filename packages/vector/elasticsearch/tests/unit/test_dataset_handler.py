"""The dataset handler never persists credentials and resolves them at connect time."""

import importlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cognee_community_vector_adapter_elasticsearch.ElasticsearchDatasetDatabaseHandler import (
    ElasticsearchDatasetDatabaseHandler,
)

# The package re-exports the class under the module's name, so patch through the
# module object itself.
handler_module = importlib.import_module(
    "cognee_community_vector_adapter_elasticsearch.ElasticsearchDatasetDatabaseHandler"
)

CONFIG = SimpleNamespace(
    vector_db_provider="elasticsearch",
    vector_db_url="https://es.internal:9200",
    vector_db_key="secret-api-key",
    vector_db_username="elastic",
    vector_db_password="changeme",
    vector_db_host="ignored-host",
    vector_db_port=1234,
)


@pytest.fixture(autouse=True)
def vector_config(monkeypatch):
    monkeypatch.setattr(handler_module, "get_vectordb_config", lambda: CONFIG)
    return CONFIG


def _row(**fields):
    return SimpleNamespace(**fields)


@pytest.mark.asyncio
async def test_create_dataset_stores_no_secrets_and_no_host_port():
    dataset_id = uuid4()
    row = await ElasticsearchDatasetDatabaseHandler.create_dataset(dataset_id, None)

    assert row["vector_database_key"] == ""
    assert row["vector_database_connection_info"] == {}
    assert row["vector_database_url"] == "https://es.internal:9200"
    assert row["vector_database_name"] == str(dataset_id)
    assert "secret-api-key" not in repr(row)
    assert "changeme" not in repr(row)


@pytest.mark.asyncio
async def test_create_dataset_refuses_another_provider(monkeypatch):
    other = SimpleNamespace(**{**vars(CONFIG), "vector_db_provider": "lancedb"})
    monkeypatch.setattr(handler_module, "get_vectordb_config", lambda: other)

    with pytest.raises(ValueError, match="Elasticsearch"):
        await ElasticsearchDatasetDatabaseHandler.create_dataset(uuid4(), None)


@pytest.mark.asyncio
async def test_resolve_fills_key_and_basic_auth_from_the_live_config():
    row = _row(vector_database_key="", vector_database_connection_info={})

    resolved = await ElasticsearchDatasetDatabaseHandler.resolve_dataset_connection_info(row)

    assert resolved.vector_database_key == "secret-api-key"
    assert resolved.vector_database_connection_info["username"] == "elastic"
    assert resolved.vector_database_connection_info["password"] == "changeme"


@pytest.mark.asyncio
async def test_resolve_tolerates_a_row_without_connection_info():
    row = _row(vector_database_key="", vector_database_connection_info=None)

    resolved = await ElasticsearchDatasetDatabaseHandler.resolve_dataset_connection_info(row)

    assert resolved.vector_database_connection_info["username"] == "elastic"


@pytest.mark.asyncio
async def test_delete_dataset_passes_credentials_without_touching_the_row(monkeypatch):
    calls = []

    class Engine:
        async def prune(self):
            calls.append("prune")

    def fake_create_vector_engine(**kwargs):
        calls.append(kwargs)
        return Engine()

    monkeypatch.setattr(handler_module, "create_vector_engine", fake_create_vector_engine)
    row = _row(
        vector_database_provider="elasticsearch",
        vector_database_url="https://es.internal:9200",
        vector_database_key="",
        vector_database_name="dataset-1",
        vector_database_connection_info={},
    )

    await ElasticsearchDatasetDatabaseHandler.delete_dataset(row)

    kwargs, done = calls
    assert done == "prune"
    assert kwargs["vector_db_key"] == "secret-api-key"
    assert kwargs["vector_db_username"] == "elastic"
    assert kwargs["vector_db_password"] == "changeme"
    assert kwargs["vector_db_name"] == "dataset-1"
    # The row may still be attached to cognee's session: nothing is written onto it.
    assert row.vector_database_key == ""
    assert row.vector_database_connection_info == {}
