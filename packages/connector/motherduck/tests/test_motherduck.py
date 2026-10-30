import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cognee_community_connector_motherduck import MotherDuckConnector

def make_async_response(status=200, json_data=None):
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data or {})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm

def test_token_validation():
    with pytest.raises(ValueError, match="token must be provided"):
        MotherDuckConnector(token="")

def test_fetch_databases():
    async def run_test():
        connector = MotherDuckConnector(token="test_token")

        db_json = {
            "databases": [
                {
                    "name": "telemetry_db",
                    "size_bytes": 1048576,
                    "owner": "analytics_team",
                    "updated_at": "2026-10-25T12:00:00Z",
                    "is_shared": True,
                }
            ]
        }

        with patch("aiohttp.ClientSession.get", return_value=make_async_response(200, db_json)):
            async with aiohttp_session() as session:
                dbs = await connector.fetch_databases(session)
                assert len(dbs) == 1
                assert dbs[0]["name"] == "telemetry_db"
                assert dbs[0]["size_bytes"] == 1048576
                assert dbs[0]["is_shared"] is True
                assert "telemetry_db" in dbs[0]["clean_text"]

    asyncio.run(run_test())

def test_fetch_shares():
    async def run_test():
        connector = MotherDuckConnector(token="test_token")

        shares_json = {
            "shares": [
                {
                    "name": "public_sample_share",
                    "database_name": "benchmark_data",
                    "access_level": "read_only",
                    "created_at": "2026-10-20T08:00:00Z",
                }
            ]
        }

        with patch("aiohttp.ClientSession.get", return_value=make_async_response(200, shares_json)):
            async with aiohttp_session() as session:
                shares = await connector.fetch_shares_list(session)
                assert len(shares) == 1
                assert shares[0]["share_name"] == "public_sample_share"
                assert shares[0]["database_name"] == "benchmark_data"
                assert "read_only" in shares[0]["clean_text"]

    asyncio.run(run_test())

def test_fetch_queries_bounded():
    async def run_test():
        connector = MotherDuckConnector(token="test_token", query_limit=2)

        queries_json = {
            "queries": [
                {
                    "id": "q_101",
                    "query_text": "SELECT count(*) FROM telemetry_events",
                    "duration_ms": 142,
                    "status": "SUCCESS",
                    "timestamp": "2026-10-25T14:20:00Z",
                    "rows_read": 500000,
                },
                {
                    "id": "q_102",
                    "query_text": "SELECT * FROM error_logs",
                    "duration_ms": 850,
                    "status": "FAILED",
                    "timestamp": "2026-10-25T14:21:00Z",
                    "rows_read": 120,
                },
                {
                    "id": "q_103",
                    "query_text": "SELECT 1",
                    "duration_ms": 5,
                    "status": "SUCCESS",
                }
            ]
        }

        with patch("aiohttp.ClientSession.get", return_value=make_async_response(200, queries_json)):
            async with aiohttp_session() as session:
                queries = await connector.fetch_recent_queries(session)
                assert len(queries) == 2  # Bounded by query_limit
                assert queries[0]["query_id"] == "q_101"
                assert queries[0]["duration_ms"] == 142
                assert "telemetry_events" in queries[0]["clean_text"]
                assert queries[1]["status"] == "FAILED"

    asyncio.run(run_test())

def test_api_error_handling():
    async def run_test():
        connector = MotherDuckConnector(token="invalid_token")

        with patch("aiohttp.ClientSession.get", return_value=make_async_response(401, {"error": "Unauthorized"})):
            async with aiohttp_session() as session:
                dbs = await connector.fetch_databases(session)
                shares = await connector.fetch_shares_list(session)
                queries = await connector.fetch_recent_queries(session)
                assert dbs == []
                assert shares == []
                assert queries == []

    asyncio.run(run_test())

def test_fetch_all_complete():
    async def run_test():
        connector = MotherDuckConnector(token="test_token")

        def mock_get(url, **kwargs):
            if "/databases" in url:
                return make_async_response(200, {"databases": [{"name": "db1"}]})
            if "/shares" in url:
                return make_async_response(200, {"shares": [{"name": "s1"}]})
            if "/queries" in url:
                return make_async_response(200, {"queries": [{"id": "q1"}]})
            return make_async_response(404, {})

        with patch("aiohttp.ClientSession.get", side_effect=mock_get):
            data = await connector.fetch_all()
            assert "databases" in data
            assert len(data["databases"]) == 1
            assert len(data["shares"]) == 1
            assert len(data["queries"]) == 1

    asyncio.run(run_test())

import aiohttp
def aiohttp_session():
    return aiohttp.ClientSession()
