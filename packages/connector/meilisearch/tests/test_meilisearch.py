import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from cognee_community_connector_meilisearch import MeilisearchConnector

def make_async_response(status=200, json_data=None):
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data or {})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm

def test_headers_auth():
    c_auth = MeilisearchConnector(api_key="secret_key")
    assert c_auth._headers()["Authorization"] == "Bearer secret_key"

    c_no_auth = MeilisearchConnector()
    assert "Authorization" not in c_no_auth._headers()

def test_get_indexes():
    async def run_test():
        connector = MeilisearchConnector(api_key="key")

        indexes_json = {
            "results": [
                {"uid": "movies", "primaryKey": "id"},
                {"uid": "books", "primaryKey": "isbn"},
            ]
        }

        with patch("aiohttp.ClientSession.get", return_value=make_async_response(200, indexes_json)):
            async with aiohttp_session() as session:
                uids = await connector.get_indexes(session)
                assert uids == ["movies", "books"]

    asyncio.run(run_test())

def test_fetch_index_documents():
    async def run_test():
        connector = MeilisearchConnector(indexes=["products"], limit_per_index=5)

        docs_json = {
            "results": [
                {
                    "id": "prod_1",
                    "title": "Ergonomic Keyboard",
                    "price": 99.99,
                    "category": "Electronics",
                }
            ]
        }

        with patch("aiohttp.ClientSession.get", return_value=make_async_response(200, docs_json)):
            async with aiohttp_session() as session:
                docs = await connector.fetch_index_documents(session, "products")
                assert len(docs) == 1
                assert docs[0]["doc_id"] == "prod_1"
                assert docs[0]["index_uid"] == "products"
                assert "Ergonomic Keyboard" in docs[0]["clean_text"]
                assert "99.99" in docs[0]["clean_text"]

    asyncio.run(run_test())

def test_field_filtering():
    async def run_test():
        connector = MeilisearchConnector(
            indexes=["users"],
            fields=["id", "username"]
        )

        doc = {"id": "u1", "username": "alice", "private_ssn": "123-45-6789"}
        formatted = connector._format_document("users", doc)
        assert "alice" in formatted["clean_text"]
        assert "private_ssn" not in formatted["clean_text"]

    asyncio.run(run_test())

def test_api_error_returns_empty():
    async def run_test():
        connector = MeilisearchConnector(api_key="bad_key")

        with patch("aiohttp.ClientSession.get", return_value=make_async_response(401, {"message": "Invalid API key"})):
            async with aiohttp_session() as session:
                docs = await connector.fetch_index_documents(session, "test")
                assert docs == []

    asyncio.run(run_test())

def test_fetch_all_auto_discover():
    async def run_test():
        connector = MeilisearchConnector()

        def mock_get(url, **kwargs):
            if "/indexes" in url and "/documents" not in url:
                return make_async_response(200, {"results": [{"uid": "catalog"}]})
            return make_async_response(200, {"results": [{"id": "item1", "name": "Item 1"}]})

        with patch("aiohttp.ClientSession.get", side_effect=mock_get):
            docs = await connector.fetch_all()
            assert len(docs) == 1
            assert docs[0]["doc_id"] == "item1"

    asyncio.run(run_test())

import aiohttp
def aiohttp_session():
    return aiohttp.ClientSession()
