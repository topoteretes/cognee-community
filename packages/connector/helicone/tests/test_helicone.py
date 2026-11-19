import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cognee_community_connector_helicone import HeliconeConnector

def make_async_response(status=200, json_data=None):
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data or {})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm

def test_validation():
    with pytest.raises(ValueError, match="Helicone API key must be provided"):
        HeliconeConnector(api_key="")

def test_headers():
    connector = HeliconeConnector(api_key="sk_helicone_test_123")
    assert connector._headers()["Authorization"] == "Bearer sk_helicone_test_123"

def test_fetch_prompts():
    async def run_test():
        connector = HeliconeConnector(api_key="k")

        prompts_json = {
            "data": [
                {
                    "id": "customer_support_v2",
                    "model": "gpt-4o",
                    "version": 2,
                    "prompt_text": "You are a helpful customer support agent for Acme Corp.",
                    "created_at": "2026-10-25T10:00:00Z",
                }
            ]
        }

        with patch("aiohttp.ClientSession.post", return_value=make_async_response(200, prompts_json)):
            async with aiohttp_session() as session:
                prompts = await connector.fetch_prompts(session)
                assert len(prompts) == 1
                p = prompts[0]
                assert p["prompt_id"] == "customer_support_v2"
                assert p["version"] == 2
                assert "gpt-4o" in p["clean_text"]
                assert "helpful customer support agent" in p["clean_text"]

    asyncio.run(run_test())

def test_fetch_datasets():
    async def run_test():
        connector = HeliconeConnector(api_key="k")

        ds_json = {
            "data": [
                {
                    "id": "ds_hallucination_cases",
                    "name": "Hallucination Benchmark Suite",
                    "description": "50 adversarial prompts testing ground-truth adherence.",
                    "rows_count": 50,
                }
            ]
        }

        with patch("aiohttp.ClientSession.post", return_value=make_async_response(200, ds_json)):
            async with aiohttp_session() as session:
                datasets = await connector.fetch_datasets(session)
                assert len(datasets) == 1
                d = datasets[0]
                assert d["dataset_id"] == "ds_hallucination_cases"
                assert d["rows_count"] == 50
                assert "Hallucination Benchmark Suite" in d["clean_text"]

    asyncio.run(run_test())

def test_fetch_evaluators():
    async def run_test():
        connector = HeliconeConnector(api_key="k")

        eval_json = {
            "data": [
                {
                    "id": "eval_relevance",
                    "name": "Answer Relevance Judge",
                    "eval_type": "llm_as_judge",
                    "scoring_type": "boolean",
                }
            ]
        }

        with patch("aiohttp.ClientSession.post", return_value=make_async_response(200, eval_json)):
            async with aiohttp_session() as session:
                evaluators = await connector.fetch_evaluators(session)
                assert len(evaluators) == 1
                assert evaluators[0]["name"] == "Answer Relevance Judge"
                assert "Answer Relevance Judge" in evaluators[0]["clean_text"]

    asyncio.run(run_test())

def test_fetch_feedback_traces():
    async def run_test():
        connector = HeliconeConnector(api_key="k")

        fb_json = {
            "data": [
                {
                    "id": "fb_101",
                    "rating": False,
                    "comment": "Bot gave outdated pricing for Enterprise plan.",
                    "request_id": "req_888",
                }
            ]
        }

        with patch("aiohttp.ClientSession.post", return_value=make_async_response(200, fb_json)):
            async with aiohttp_session() as session:
                feedbacks = await connector.fetch_feedback_traces(session)
                assert len(feedbacks) == 1
                fb = feedbacks[0]
                assert fb["rating"] == "Negative (Thumbs Down)"
                assert "outdated pricing for Enterprise plan" in fb["clean_text"]

    asyncio.run(run_test())

def test_api_error_handling():
    async def run_test():
        connector = HeliconeConnector(api_key="bad_key")

        with patch("aiohttp.ClientSession.post", return_value=make_async_response(401, {"error": "Unauthorized"})):
            async with aiohttp_session() as session:
                p = await connector.fetch_prompts(session)
                d = await connector.fetch_datasets(session)
                e = await connector.fetch_evaluators(session)
                f = await connector.fetch_feedback_traces(session)
                assert p == []
                assert d == []
                assert e == []
                assert f == []

    asyncio.run(run_test())

def test_fetch_all_complete():
    async def run_test():
        connector = HeliconeConnector(api_key="k")

        def mock_post(url, **kwargs):
            if "/prompt/" in url:
                return make_async_response(200, {"data": [{"id": "p1"}]})
            if "/helicone-dataset/" in url:
                return make_async_response(200, {"data": [{"id": "d1"}]})
            if "/evaluator/" in url:
                return make_async_response(200, {"data": [{"id": "e1"}]})
            if "/feedback/" in url:
                return make_async_response(200, {"data": [{"id": "f1"}]})
            return make_async_response(404, {})

        with patch("aiohttp.ClientSession.post", side_effect=mock_post):
            data = await connector.fetch_all()
            assert len(data["prompts"]) == 1
            assert len(data["datasets"]) == 1
            assert len(data["evaluators"]) == 1
            assert len(data["feedbacks"]) == 1

    asyncio.run(run_test())

import aiohttp
def aiohttp_session():
    return aiohttp.ClientSession()
