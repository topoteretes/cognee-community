import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cognee_community_connector_surrealdb import SurrealDBConnector

def make_async_response(status=200, json_data=None):
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data or [])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm

def test_validation():
    with pytest.raises(ValueError, match="namespace .* and database"):
        SurrealDBConnector(namespace="", database="")

def test_headers_auth():
    c_token = SurrealDBConnector(namespace="ns1", database="db1", token="my_jwt")
    h1 = c_token._headers()
    assert h1["NS"] == "ns1"
    assert h1["DB"] == "db1"
    assert h1["Authorization"] == "Bearer my_jwt"

    c_basic = SurrealDBConnector(namespace="ns2", database="db2", username="root", password="pwd")
    h2 = c_basic._headers()
    assert "Basic" in h2["Authorization"]

def test_get_tables_info():
    async def run_test():
        connector = SurrealDBConnector(namespace="ns", database="db", token="t")

        info_json = [
            {
                "status": "OK",
                "result": {
                    "tables": {
                        "user": "DEFINE TABLE user ...",
                        "post": "DEFINE TABLE post ...",
                    }
                }
            }
        ]

        with patch("aiohttp.ClientSession.post", return_value=make_async_response(200, info_json)):
            async with aiohttp_session() as session:
                tables = await connector.get_tables_info(session)
                assert "user" in tables
                assert "post" in tables

    asyncio.run(run_test())

def test_fetch_table_records_with_edges():
    async def run_test():
        connector = SurrealDBConnector(namespace="ns", database="db", token="t", tables=["purchased"])

        query_json = [
            {
                "status": "OK",
                "result": [
                    {
                        "id": "purchased:order_99",
                        "in": "user:alice",
                        "out": "product:laptop",
                        "price": 1299.99,
                        "currency": "USD",
                    }
                ]
            }
        ]

        with patch("aiohttp.ClientSession.post", return_value=make_async_response(200, query_json)):
            async with aiohttp_session() as session:
                records = await connector.fetch_table_records(session, "purchased")
                assert len(records) == 1
                rec = records[0]
                assert rec["table"] == "purchased"
                assert rec["record_id"] == "purchased:order_99"
                assert rec["edge_in"] == "user:alice"
                assert rec["edge_out"] == "product:laptop"
                assert "Incoming Edge (in): `user:alice`" in rec["clean_text"]
                assert "Outgoing Edge (out): `product:laptop`" in rec["clean_text"]
                assert "1299.99" in rec["clean_text"]

    asyncio.run(run_test())

def test_error_handling_returns_empty():
    async def run_test():
        connector = SurrealDBConnector(namespace="ns", database="db", token="bad_token", tables=["users"])

        with patch("aiohttp.ClientSession.post", return_value=make_async_response(401, {"code": 401, "message": "Unauthorized"})):
            async with aiohttp_session() as session:
                records = await connector.fetch_table_records(session, "users")
                assert records == []

    asyncio.run(run_test())

def test_fetch_all_auto_discovery():
    async def run_test():
        connector = SurrealDBConnector(namespace="ns", database="db", token="t")

        def mock_post(url, data, **kwargs):
            if "INFO FOR DB" in data:
                return make_async_response(200, [{"status": "OK", "result": {"tables": {"account": {}}}}])
            return make_async_response(200, [{"status": "OK", "result": [{"id": "account:1", "name": "Corp"}]}])

        with patch("aiohttp.ClientSession.post", side_effect=mock_post):
            docs = await connector.fetch_all()
            assert len(docs) == 1
            assert docs[0]["table"] == "account"
            assert docs[0]["record_id"] == "account:1"

    asyncio.run(run_test())

import aiohttp
def aiohttp_session():
    return aiohttp.ClientSession()
