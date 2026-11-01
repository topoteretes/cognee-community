import asyncio
from cognee_community_connector_motherduck import MotherDuckConnector

async def main():
    connector = MotherDuckConnector(
        token="md_your_motherduck_access_token",
        fetch_queries=True,
        query_limit=10,
    )

    print("Fetching databases, shares, and query telemetry from MotherDuck...")
    data = await connector.fetch_all()

    print(f"Databases ({len(data['databases'])}):")
    for db in data["databases"]:
        print(f" - {db['name']} (Size: {db['size_bytes']} bytes)")

    print(f"\nShares ({len(data['shares'])}):")
    for s in data["shares"]:
        print(f" - {s['share_name']} -> {s['database_name']}")

    print(f"\nQueries ({len(data['queries'])}):")
    for q in data["queries"]:
        print(f" - [{q['status']}] {q['query_id']} ({q['duration_ms']}ms): {q['sql'][:40]}...")

if __name__ == "__main__":
    asyncio.run(main())
