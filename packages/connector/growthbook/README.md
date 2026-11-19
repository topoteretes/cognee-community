# Cognee Community Connector: GrowthBook

Community data-source connector for [GrowthBook](https://www.growthbook.io/) open-source feature flags, A/B experiments, hypotheses, variation outcomes, and metric definitions into [Cognee](https://github.com/topoteretes/cognee).

## Overview
GrowthBook is the leading open-source platform for feature management and experimentation. While traditional feature-flag systems track raw toggles, GrowthBook records rich experiment hypotheses, variation specifications, statistical conclusions, and metric definitions.

Ingesting GrowthBook into Cognee enables AI agents to reason about product experiments (e.g. *Which A/B experiment tested checkout conversion last quarter?*, *What was the hypothesis and which variant won?*, *Which feature flags gate active experiments right now?*).

## Features
- **Feature Flag Tracking:** Ingests feature flag IDs, value types, default values, and environment rules.
- **A/B Experiment Intelligence:** Ingests hypotheses, variations, status states, winning variants, and statistical conclusions.
- **Metric Definitions:** Captures custom business metrics (conversion, count, binomial) and definitions.
- **Cognitive Document Synthesis:** Emits clean semantic markdown documents ready for LLM vectorization and Graph RAG reasoning.
- **100% Hermetic Testing:** Tested offline with zero live GrowthBook server requirements.

## Installation
```bash
pip install cognee-community-connector-growthbook
```

## Quickstart
```python
import asyncio
from cognee_community_connector_growthbook import GrowthBookConnector
import cognee

async def main():
    connector = GrowthBookConnector(
        api_key="your_growthbook_api_key",
        base_url="https://api.growthbook.io",
        fetch_features_flag=True,
        fetch_experiments_flag=True,
        fetch_metrics_flag=True,
    )

    data = await connector.fetch_all()
    print(f"Retrieved {len(data['features'])} flags and {len(data['experiments'])} experiments.")

    # Ingest into Cognee Knowledge Graph
    for f in data["features"]:
        await cognee.add(f["clean_text"])
    
    for exp in data["experiments"]:
        await cognee.add(exp["clean_text"])

    await cognee.cognify()
    print("Ingestion complete!")

if __name__ == "__main__":
    asyncio.run(main())
```
