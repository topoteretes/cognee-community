# Cognee Community Connector: InfluxDB

Community data-source connector for [InfluxDB v2](https://www.influxdata.com/) time-series telemetry and aggregated metrics into [Cognee](https://github.com/topoteretes/cognee).

## Overview
InfluxDB is the industry-standard time-series platform for IoT, server telemetry, and industrial sensor data. High-frequency time-series datasets can contain millions of raw points per minute, which would rapidly saturate and explode LLM graph memory.

This connector queries InfluxDB v2 via official Flux HTTP endpoints and folds high-frequency points into statistical telemetry documents (capturing min, max, mean, count, and threshold breach alerts) formatted for Cognee Graph RAG memory without graph bloating.

## Features
- **Flux Query Synthesis:** Generates parameterized Flux queries targeting buckets, ranges, and allowlisted measurements.
- **Statistical Aggregation:** Automatically folds points into mathematical summaries (min, max, mean, latest value).
- **Threshold Breach Alerts:** Automatically tags threshold violations (e.g. *temperature > 25°C*, *CPU usage > 90%*).
- **Graph RAG Prose Formatting:** Emits clean semantic markdown documents ready for vector search and reasoning.
- **100% Hermetic Testing:** Tested offline with zero live InfluxDB daemon requirements.

## Installation
```bash
pip install cognee-community-connector-influxdb
```

## Quickstart
```python
import asyncio
from cognee_community_connector_influxdb import InfluxDBConnector
import cognee

async def main():
    connector = InfluxDBConnector(
        url="http://localhost:8086",
        token="my_token",
        org="my_org",
        bucket="telemetry",
        measurements=["cpu", "memory", "sensor_temp"],
        range_days=7,
        threshold_rules={"temp": 28.0},
    )

    summaries = await connector.fetch_all()
    print(f"Extracted {len(summaries)} aggregated time-series telemetry cards.")

    for s in summaries:
        await cognee.add(s["clean_text"])

    await cognee.cognify()
    print("Ingestion complete!")

if __name__ == "__main__":
    asyncio.run(main())
```
