# Cognee Honeycomb Connector

A `dlt`-backed data-source connector for [Honeycomb](https://honeycomb.io) observability datasets, boards, triggers, and SLO definitions into Cognee's entity knowledge graph.

## Installation

```bash
pip install cognee-community-connector-honeycomb
```

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_honeycomb import honeycomb_source


async def main():
    source = honeycomb_source(
        api_key="YOUR_HONEYCOMB_API_KEY",
        include_datasets=True,
        include_boards=True,
        include_triggers=True,
        include_slos=True,
    )

    await cognee.add(source, dataset_name="observability_graph")
    await cognee.cognify(dataset_name="observability_graph")

    results = await cognee.search(
        "Which alert triggers monitor database query latency exceeding 500ms?",
        dataset_name="observability_graph",
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

## Features
- **Observability Knowledge Graph**: Transforms datasets, dashboards/boards, alert triggers, and SLO contracts into structured prose documents (`DOCUMENT_SOURCE_ATTR = "honeycomb"`).
- **Incremental Sync**: Supports `since` filtering by last updated/written timestamp.
- **Idempotency**: Built with `write_disposition="replace"` for clean sync and state management.
