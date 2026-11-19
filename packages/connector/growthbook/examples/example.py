import asyncio
from cognee_community_connector_growthbook import GrowthBookConnector

async def main():
    connector = GrowthBookConnector(
        api_key="key_prod_your_growthbook_api_key",
        base_url="https://api.growthbook.io",
        fetch_features_flag=True,
        fetch_experiments_flag=True,
        fetch_metrics_flag=True,
        limit=10,
    )

    print("Fetching feature flags, experiments, and metrics from GrowthBook...")
    data = await connector.fetch_all()

    print(f"Features ({len(data['features'])}):")
    for f in data["features"]:
        print(f" - [{f['value_type']}] {f['feature_id']} (Default: {f['default_value']})")

    print(f"\nExperiments ({len(data['experiments'])}):")
    for exp in data["experiments"]:
        print(f" - [{exp['status']}] {exp['name']}: Winner={exp['winner']}")

    print(f"\nMetrics ({len(data['metrics'])}):")
    for m in data["metrics"]:
        print(f" - [{m['type']}] {m['name']}")

if __name__ == "__main__":
    asyncio.run(main())
