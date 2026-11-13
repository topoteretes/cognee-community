"""Exercise the real Cognee factory with an offline embedding engine."""

from importlib import import_module
from unittest.mock import AsyncMock

import pytest
from chromadb.config import DEFAULT_DATABASE
from cognee_community_vector_adapter_chromadb import ChromaDBAdapter
from contract_suite import FakeEmbeddingEngine, assert_vector_contract


def test_conforms_to_cognee_vector_contract():
    assert_vector_contract(ChromaDBAdapter)


def test_cognee_factory_constructs_chromadb(monkeypatch):
    factory = import_module("cognee.infrastructure.databases.vector.create_vector_engine")
    embedding_engine = FakeEmbeddingEngine()
    monkeypatch.setitem(factory.supported_databases, "chromadb", ChromaDBAdapter)
    monkeypatch.setattr(factory, "get_embedding_engine", lambda: embedding_engine)

    adapter = factory.create_vector_engine(
        vector_db_provider="chromadb",
        vector_db_url="http://localhost:8000",
        vector_db_name="contract_db",
        vector_db_host="localhost",
        vector_db_port="8000",
    )

    assert isinstance(adapter, ChromaDBAdapter)
    assert adapter.embedding_engine is embedding_engine
    assert adapter.database_name == "contract_db"


@pytest.mark.asyncio
@pytest.mark.parametrize("database_name", [None, "", "selected_db"])
async def test_connection_uses_selected_database(monkeypatch, database_name):
    module = import_module("cognee_community_vector_adapter_chromadb.chromadb_adapter")
    client = AsyncMock()
    connect = AsyncMock(return_value=client)
    monkeypatch.setattr(module, "AsyncHttpClient", connect)
    adapter = ChromaDBAdapter(
        url="http://localhost:8000",
        api_key="test-token",
        embedding_engine=FakeEmbeddingEngine(),
        database_name=database_name,
    )

    assert await adapter.get_connection() is client
    assert await adapter.get_connection() is client
    connect.assert_awaited_once()
    assert connect.call_args.kwargs["database"] == (database_name or DEFAULT_DATABASE)
