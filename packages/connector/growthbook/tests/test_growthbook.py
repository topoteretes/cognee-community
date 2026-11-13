import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cognee_community_connector_growthbook import GrowthBookConnector

def make_async_response(status=200, json_data=None):
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data or {})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm

def test_validation():
    with pytest.raises(ValueError, match="GrowthBook API key must be provided"):
        GrowthBookConnector(api_key="")

def test_headers():
    connector = GrowthBookConnector(api_key="gb_secret_123")
    assert connector._headers()["Authorization"] == "Bearer gb_secret_123"

def test_fetch_features():
    async def run_test():
        connector = GrowthBookConnector(api_key="k")

        features_json = {
            "features": [
                {
                    "id": "new_checkout_flow",
                    "description": "Enables multi-step checkout with one-click payment.",
                    "defaultValue": "false",
                    "valueType": "boolean",
                    "tags": ["checkout", "growth"],
                    "dateUpdated": "2026-10-25T14:00:00Z",
                }
            ]
        }

        with patch("aiohttp.ClientSession.get", return_value=make_async_response(200, features_json)):
            async with aiohttp_session() as session:
                features = await connector.fetch_features(session)
                assert len(features) == 1
                f = features[0]
                assert f["feature_id"] == "new_checkout_flow"
                assert f["value_type"] == "boolean"
                assert "new_checkout_flow" in f["clean_text"]
                assert "multi-step checkout" in f["clean_text"]

    asyncio.run(run_test())

def test_fetch_experiments_with_results():
    async def run_test():
        connector = GrowthBookConnector(api_key="k")

        exp_json = {
            "experiments": [
                {
                    "id": "exp_checkout_cta",
                    "name": "Checkout CTA Color Test",
                    "status": "stopped",
                    "hypothesis": "Bright green CTA increases checkout conversion by 8%.",
                    "variations": [
                        {"name": "Control (Blue)", "description": "Standard theme"},
                        {"name": "Treatment (Green)", "description": "Emerald button"},
                    ],
                    "resultSummary": {
                        "winner": "Treatment (Green)",
                        "conclusion": "Green CTA statistically increased checkout conversion by 9.2% with 99% confidence.",
                    }
                }
            ]
        }

        with patch("aiohttp.ClientSession.get", return_value=make_async_response(200, exp_json)):
            async with aiohttp_session() as session:
                experiments = await connector.fetch_experiments(session)
                assert len(experiments) == 1
                exp = experiments[0]
                assert exp["experiment_id"] == "exp_checkout_cta"
                assert exp["winner"] == "Treatment (Green)"
                assert "Bright green CTA increases checkout conversion" in exp["clean_text"]
                assert "statistically increased checkout conversion by 9.2%" in exp["clean_text"]

    asyncio.run(run_test())

def test_fetch_metrics():
    async def run_test():
        connector = GrowthBookConnector(api_key="k")

        metrics_json = {
            "metrics": [
                {
                    "id": "met_signup_conversion",
                    "name": "Signup Conversion Rate",
                    "type": "binomial",
                    "description": "Proportion of visitors who complete the registration flow.",
                }
            ]
        }

        with patch("aiohttp.ClientSession.get", return_value=make_async_response(200, metrics_json)):
            async with aiohttp_session() as session:
                metrics = await connector.fetch_metrics(session)
                assert len(metrics) == 1
                m = metrics[0]
                assert m["metric_id"] == "met_signup_conversion"
                assert m["type"] == "binomial"
                assert "Signup Conversion Rate" in m["clean_text"]

    asyncio.run(run_test())

def test_api_error_handling():
    async def run_test():
        connector = GrowthBookConnector(api_key="bad_key")

        with patch("aiohttp.ClientSession.get", return_value=make_async_response(401, {"message": "Invalid token"})):
            async with aiohttp_session() as session:
                f = await connector.fetch_features(session)
                e = await connector.fetch_experiments(session)
                m = await connector.fetch_metrics(session)
                assert f == []
                assert e == []
                assert m == []

    asyncio.run(run_test())

def test_fetch_all_complete():
    async def run_test():
        connector = GrowthBookConnector(api_key="k")

        def mock_get(url, **kwargs):
            if "/features" in url:
                return make_async_response(200, {"features": [{"id": "f1"}]})
            if "/experiments" in url:
                return make_async_response(200, {"experiments": [{"id": "e1"}]})
            if "/metrics" in url:
                return make_async_response(200, {"metrics": [{"id": "m1"}]})
            return make_async_response(404, {})

        with patch("aiohttp.ClientSession.get", side_effect=mock_get):
            data = await connector.fetch_all()
            assert len(data["features"]) == 1
            assert len(data["experiments"]) == 1
            assert len(data["metrics"]) == 1

    asyncio.run(run_test())

import aiohttp
def aiohttp_session():
    return aiohttp.ClientSession()
