"""Run the async HTTP adapter against an in-process Chroma server."""

import httpx
import pytest
from chromadb.config import Settings
from chromadb.errors import ChromaAuthError
from chromadb.server.fastapi import FastAPI
from cognee.infrastructure.engine import DataPoint
from cognee_community_vector_adapter_chromadb import ChromaDBAdapter
from contract_suite import FakeEmbeddingEngine


class Document(DataPoint):
    text: str
    metadata: dict = {"index_fields": ["text"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key", "server_api_key"),
    [(None, None), ("roundtrip-test-token", "roundtrip-test-token"), ("wrong", "required")],
)
@pytest.mark.parametrize("database_name", [None, "selected_db"])
async def test_http_roundtrip(monkeypatch, tmp_path, api_key, server_api_key, database_name):
    settings = Settings(
        is_persistent=True,
        persist_directory=str(tmp_path),
        anonymized_telemetry=False,
    )
    if server_api_key:
        settings.chroma_server_authn_provider = (
            "chromadb.auth.token_authn.TokenAuthenticationServerProvider"
        )
        settings.chroma_server_authn_credentials = server_api_key
    server = FastAPI(settings)
    if database_name:
        server._api.create_database(database_name)
    transport = httpx.ASGITransport(app=server.app())
    original_client = httpx.AsyncClient
    clients = []

    def local_client(*args, **kwargs):
        client = original_client(*args, transport=transport, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", local_client)
    adapter = ChromaDBAdapter(
        url="http://chroma.test",
        api_key=api_key,
        embedding_engine=FakeEmbeddingEngine(),
        database_name=database_name,
    )
    document = Document(text="Cognee stores documents")

    try:
        if api_key != server_api_key:
            with pytest.raises(ChromaAuthError):
                await adapter.get_connection()
            return
        await adapter.create_data_points("documents", [document])
        results = await adapter.retrieve("documents", [str(document.id)])
        assert len(results) == 1
        assert results[0].id == document.id
        assert results[0].payload["text"] == document.text

        results = await adapter.search("documents", document.text, limit=None, include_payload=True)
        assert len(results) == 1
        assert results[0].id == document.id

        batches = await adapter.batch_search(
            "documents", [document.text], limit=None, include_payload=True
        )
        assert len(batches) == 1
        assert len(batches[0]) == 1
        assert batches[0][0].id == document.id

        await adapter.delete_data_points("documents", [str(document.id)])
        assert await adapter.retrieve("documents", [str(document.id)]) == []
        assert await adapter.batch_search("documents", [document.text], limit=None) == [[]]
        assert await adapter.batch_search("documents", [], limit=None) == []
    finally:
        for client in clients:
            await client.aclose()
        server._system.stop()
