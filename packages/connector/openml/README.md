# Cognee Community Connector: OpenML

Community data-source connector for [OpenML](https://www.openml.org/) dataset and benchmark metadata ingestion into [Cognee](https://github.com/topoteretes/cognee).

## Overview
OpenML is the world's largest open science platform for machine learning data, tasks, and benchmarks. Selecting suitable tabular ML benchmarks requires keeping dataset versions, target attributes, statistical qualities, citations, licensing, and lifecycle statuses synchronized.

This connector connects to the official OpenML REST API anonymously to ingest dataset metadata without downloading heavy binary data files or executing ML runs.

## Features
- **Anonymous Public Ingestion:** Fetches open benchmark metadata without requiring mandatory credentials.
- **Statistical Qualities Extraction:** Collects instance counts, feature dimensions, class labels, and missing value metrics.
- **Target Attribute Resolution:** Identifies declared classification or regression target columns.
- **Lifecycle & Licensing Visibility:** Retains active and deactivated dataset visibility, licensing tags, and academic citations.
- **Deterministic Knowledge Representation:** Creates clean semantic document cards formatted for Cognee Graph RAG memory.
- **100% Hermetic Testing:** Tested offline with zero live network dependencies.

## Installation
```bash
pip install cognee-community-connector-openml
```

## Quickstart
```python
import asyncio
from cognee_community_connector_openml import OpenMLConnector
import cognee

async def main():
    # Configure benchmark dataset IDs (e.g., Iris: 61, German Credit: 31)
    connector = OpenMLConnector(
        dataset_ids=[61, 31],
        include_qualities=True,
    )

    datasets = await connector.fetch_all()
    for ds in datasets:
        print(f"Ingesting: {ds['name']} (Target: {ds['target_attribute']})")
        await cognee.add(ds["clean_text"])

    await cognee.cognify()
    print("Ingestion complete!")

if __name__ == "__main__":
    asyncio.run(main())
```
