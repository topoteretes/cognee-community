# Cognee Community Connector: Linear

A production-ready community data-source connector for [Linear](https://linear.app). Ingests issues, team roadmaps, priorities, assignees, and engineering context into [Cognee](https://github.com/topoteretes/cognee) as structured documents for AI agent memory and graph reasoning.

---

## Capabilities

* **Engineering Task & Issue Memory**: Ingests issue identifiers (`ENG-123`), titles, descriptions, priorities, workflow statuses, assignees, and labels.
* **Document Mode Support**: Marks resources with `DOCUMENT_SOURCE_ATTR = "linear"` to route issues through Cognee's cognify entity-extraction pipeline.
* **Full Snapshot with Forget-on-Delete**: Synchronizes with `write_disposition="replace"`. Deleted or canceled issues in Linear automatically drop out of the snapshot, triggering Cognee's `orphan_cleanup` to prune them from the knowledge graph and vector stores.
* **Resilient GraphQL Transport**: Automatically handles rate limiting (HTTP 429) with exponential backoff and `Retry-After` header parsing.

---

## Installation

```bash
pip install cognee-community-connector-linear
```

Or from source:

```bash
pip install -e packages/connector/linear
```

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_linear import linear_source


async def main():
    # Configure Cognee dataset
    dataset_name = "engineering_team_memory"

    # Create Linear source (reads LINEAR_API_KEY from environment or explicit argument)
    source = linear_source(
        api_key=os.environ.get("LINEAR_API_KEY"),
        team_keys=["ENG"],  # Optional: restrict to specific teams
    )

    # Ingest and cognify into memory
    print("Syncing Linear issues into Cognee memory ...")
    await cognee.remember(source, dataset_name=dataset_name)

    # Search knowledge graph for engineering issues
    answer = await cognee.search(
        query_text="What are the critical bugs blocking our current release?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[dataset_name],
    )
    print("Graph search result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

```bash
pytest packages/connector/linear/tests
```
