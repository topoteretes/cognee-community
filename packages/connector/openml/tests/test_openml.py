import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from cognee_community_connector_openml import OpenMLConnector

def make_async_response(status=200, json_data=None):
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data or {})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm

def test_fetch_dataset_success():
    async def run_test():
        connector = OpenMLConnector(dataset_ids=[61], include_qualities=True)

        meta_json = {
            "data_set_description": {
                "id": "61",
                "name": "iris",
                "version": "1",
                "description": "Iris Plants Database",
                "default_target_attribute": "class",
                "licence": "Public",
                "status": "active",
                "tag": ["study_1", "benchmark"],
                "citation": "Fisher, R.A. (1936)",
                "url": "https://www.openml.org/d/61",
            }
        }
        qualities_json = {
            "data_qualities": {
                "quality": [
                    {"name": "NumberOfInstances", "value": "150"},
                    {"name": "NumberOfFeatures", "value": "5"},
                    {"name": "NumberOfClasses", "value": "3"},
                    {"name": "NumberOfMissingValues", "value": "0"},
                ]
            }
        }

        def mock_get(url, **kwargs):
            if "/qualities/" in url:
                return make_async_response(200, qualities_json)
            return make_async_response(200, meta_json)

        with patch("aiohttp.ClientSession.get", side_effect=mock_get):
            doc = await connector.fetch_dataset(61)
            assert doc is not None
            assert doc["dataset_id"] == "61"
            assert doc["name"] == "iris"
            assert doc["target_attribute"] == "class"
            assert doc["status"] == "active"
            assert doc["qualities"]["NumberOfInstances"] == "150"
            assert "NumberOfFeatures: 5" in doc["clean_text"]
            assert "class" in doc["clean_text"]

    asyncio.run(run_test())

def test_fetch_deactivated_dataset():
    async def run_test():
        connector = OpenMLConnector(dataset_ids=[999], include_qualities=False)

        meta_json = {
            "data_set_description": {
                "id": "999",
                "name": "legacy_experiment",
                "version": "2",
                "description": "Deactivated older version",
                "status": "deactivated",
            }
        }

        with patch("aiohttp.ClientSession.get", return_value=make_async_response(200, meta_json)):
            doc = await connector.fetch_dataset(999)
            assert doc is not None
            assert doc["status"] == "deactivated"
            assert "Status: deactivated" in doc["clean_text"]

    asyncio.run(run_test())

def test_fetch_absent_qualities():
    async def run_test():
        connector = OpenMLConnector(dataset_ids=[42], include_qualities=True)

        meta_json = {
            "data_set_description": {
                "id": "42",
                "name": "soybean",
                "version": "1",
                "status": "active",
            }
        }

        def mock_get(url, **kwargs):
            if "/qualities/" in url:
                return make_async_response(404, {"error": {"message": "No qualities found"}})
            return make_async_response(200, meta_json)

        with patch("aiohttp.ClientSession.get", side_effect=mock_get):
            doc = await connector.fetch_dataset(42)
            assert doc is not None
            assert doc["qualities"] == {}
            assert "No reported qualities" in doc["clean_text"]

    asyncio.run(run_test())

def test_api_error_returns_none():
    async def run_test():
        connector = OpenMLConnector(dataset_ids=[99999])

        with patch("aiohttp.ClientSession.get", return_value=make_async_response(404, {"error": "Not Found"})):
            doc = await connector.fetch_dataset(99999)
            assert doc is None

    asyncio.run(run_test())

def test_empty_dataset_ids():
    async def run_test():
        connector = OpenMLConnector(dataset_ids=[])
        docs = await connector.fetch_all()
        assert docs == []

    asyncio.run(run_test())

def test_fetch_all_multiple_datasets():
    async def run_test():
        connector = OpenMLConnector(dataset_ids=[61, 31], include_qualities=False)

        def mock_get(url, **kwargs):
            if "/data/61" in url:
                return make_async_response(200, {"data_set_description": {"id": "61", "name": "iris"}})
            if "/data/31" in url:
                return make_async_response(200, {"data_set_description": {"id": "31", "name": "credit"}})
            return make_async_response(404, {})

        with patch("aiohttp.ClientSession.get", side_effect=mock_get):
            docs = await connector.fetch_all()
            assert len(docs) == 2
            ids = [d["dataset_id"] for d in docs]
            assert "61" in ids
            assert "31" in ids

    asyncio.run(run_test())
