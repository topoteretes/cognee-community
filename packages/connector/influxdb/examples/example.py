import asyncio
from cognee_community_connector_influxdb import InfluxDBConnector

async def main():
    connector = InfluxDBConnector(
        url="http://localhost:8086",
        token="my_influxdb_v2_token",
        org="my_org",
        bucket="system_telemetry",
        measurements=["cpu", "memory", "cold_room"],
        range_days=7,
        threshold_rules={"temp": 8.0, "usage_idle": 10.0},
    )

    print("Querying InfluxDB v2 Flux endpoint and aggregating telemetry...")
    summaries = await connector.fetch_all()
    print(f"Synthesized {len(summaries)} time-series telemetry document cards.\n")

    for s in summaries:
        print(f"Metric: {s['measurement']}.{s['field']}")
        print(f"  Count: {s['count']} | Mean: {s['mean']:.2f} | Peak: {s['max']:.2f}")
        if s['breaches']:
            print(f"  ⚠️  Alerts: {s['breaches']}")
        print("-" * 50)

if __name__ == "__main__":
    asyncio.run(main())
