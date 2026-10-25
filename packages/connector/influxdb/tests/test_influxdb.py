import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cognee_community_connector_influxdb import InfluxDBConnector

def make_async_response(status=200, text_data=""):
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.text = AsyncMock(return_value=text_data)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm

def test_validation():
    with pytest.raises(ValueError, match="token, org, and bucket must be provided"):
        InfluxDBConnector(token="", org="", bucket="")

def test_flux_query_builder():
    connector = InfluxDBConnector(token="t", org="o", bucket="b", range_days=14)
    query = connector.build_flux_query("cpu")
    assert 'from(bucket: "b")' in query
    assert 'range(start: -14d)' in query
    assert 'r["_measurement"] == "cpu"' in query

def test_parse_csv_and_aggregate():
    connector = InfluxDBConnector(
        token="t", org="o", bucket="iot",
        threshold_rules={"temp": 25.0}
    )

    sample_csv = """#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double,string,string
#group,false,false,true,true,false,false,true,true
#default,_result,,,,,,,
,result,table,_start,_stop,_time,_value,_field,_measurement
,,0,2026-10-18T00:00:00Z,2026-10-25T00:00:00Z,2026-10-25T10:00:00Z,21.5,temp,cold_room
,,0,2026-10-18T00:00:00Z,2026-10-25T00:00:00Z,2026-10-25T11:00:00Z,28.0,temp,cold_room
,,0,2026-10-18T00:00:00Z,2026-10-25T00:00:00Z,2026-10-25T12:00:00Z,19.5,temp,cold_room
"""

    rows = connector.parse_flux_csv(sample_csv)
    assert len(rows) == 3
    assert rows[0]["_field"] == "temp"
    assert rows[0]["_value"] == "21.5"

    summaries = connector.aggregate_points_by_series(rows)
    assert len(summaries) == 1
    s = summaries[0]
    assert s["measurement"] == "cold_room"
    assert s["field"] == "temp"
    assert s["count"] == 3
    assert s["min"] == 19.5
    assert s["max"] == 28.0
    assert abs(s["mean"] - 23.0) < 0.01
    assert len(s["breaches"]) == 1
    assert "exceeded threshold limit of 25.0" in s["breaches"][0]
    assert "Peak: 28.0" in s["clean_text"]

def test_fetch_telemetry_api():
    async def run_test():
        connector = InfluxDBConnector(token="t", org="o", bucket="b")

        sample_csv = """,result,table,_start,_stop,_time,_value,_field,_measurement
,,0,2026-10-20Z,2026-10-25Z,2026-10-25T10:00:00Z,45.2,usage,cpu
,,0,2026-10-20Z,2026-10-25Z,2026-10-25T11:00:00Z,55.8,usage,cpu
"""

        with patch("aiohttp.ClientSession.post", return_value=make_async_response(200, sample_csv)):
            async with aiohttp_session() as session:
                res = await connector.fetch_telemetry(session, "cpu")
                assert len(res) == 1
                assert res[0]["count"] == 2
                assert res[0]["mean"] == 50.5
                assert "**Average (Mean)**: 50.50" in res[0]["clean_text"]

    asyncio.run(run_test())

def test_api_error_returns_empty():
    async def run_test():
        connector = InfluxDBConnector(token="bad_token", org="o", bucket="b")

        with patch("aiohttp.ClientSession.post", return_value=make_async_response(401, "unauthorized")):
            async with aiohttp_session() as session:
                res = await connector.fetch_telemetry(session, "cpu")
                assert res == []

    asyncio.run(run_test())

import aiohttp
def aiohttp_session():
    return aiohttp.ClientSession()
