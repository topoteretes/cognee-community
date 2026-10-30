# Cognee Community Graph Adapter - Google Cloud Spanner (Spanner Graph)

This package provides a Google Cloud Spanner graph database adapter for Cognee using **Spanner Graph** (GQL). It allows using Cloud Spanner as the graph backend for knowledge graphs and GraphRAG pipelines when running in GCP.

## Requirements

- Python >= 3.10, <= 3.13
- Google Cloud project with Spanner API enabled
- A Spanner instance (Enterprise or Enterprise Plus edition; Spanner Graph is not available on PostgreSQL dialect)
- A database created with the Cognee graph schema (see below)

## Schema setup

Create a Spanner database with **Google Standard SQL** and run the following DDL to create the Cognee graph tables and property graph:

```sql
CREATE TABLE CogneeNode (
  id         STRING(MAX) NOT NULL,
  properties JSON,
) PRIMARY KEY (id);

CREATE TABLE CogneeEdge (
  source_id         STRING(MAX) NOT NULL,
  target_id         STRING(MAX) NOT NULL,
  edge_id           STRING(MAX) NOT NULL,
  relationship_type STRING(MAX) NOT NULL,
  properties        JSON,
) PRIMARY KEY (source_id, target_id, edge_id);

CREATE OR REPLACE PROPERTY GRAPH CogneeGraph
  NODE TABLES (CogneeNode)
  EDGE TABLES (
    CogneeEdge
      SOURCE KEY (source_id) REFERENCES CogneeNode (id)
      DESTINATION KEY (target_id) REFERENCES CogneeNode (id)
      LABEL Related
  );
```

You can create the database and apply this DDL via the [Google Cloud Console](https://console.cloud.google.com/spanner), gcloud CLI, or the Spanner Admin API. See [Set up and query Spanner Graph](https://cloud.google.com/spanner/docs/graph/set-up) for details.

## Installation

Create a virtual environment, then install the package (recommended):

```bash
cd packages/graph/spanner
python -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install -e ".[dev]"   # Editable install with dev deps (pytest, pytest-asyncio)
```

Or install from PyPI (when published):

```bash
pip install cognee-community-graph-adapter-spanner
```

Or with uv:

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

## Usage

```python
import asyncio
import cognee
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee_community_graph_adapter_spanner import register


async def main():
    register()
    cognee.config.set_graph_database_provider("spanner")

    # Option A: single URL (project_id/instance_id/database_id)
    cognee.config.set_graph_db_config(
        {
            "graph_database_url": "my-project/my-instance/my-database",
        }
    )

    # Option B: separate parameters
    cognee.config.set_graph_db_config(
        {
            "project_id": "my-project",
            "instance_id": "my-instance",
            "database_id": "my-database",
        }
    )

    # Optional: custom credentials (defaults to Application Default Credentials)
    # cognee.config.set_graph_db_config({
    #     "project_id": "my-project",
    #     "instance_id": "my-instance",
    #     "database_id": "my-database",
    #     "credentials": your_credentials,
    # })

    await cognee.add(["Your content here."], "my_dataset")
    await cognee.cognify(["my_dataset"])
    results = await cognee.search(
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        query_text="your query",
    )
    graph_engine = await get_graph_engine()
    nodes, edges = await graph_engine.get_graph_data()


if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

Configure via `set_graph_db_config()` with one of:

| Key | Description |
|-----|-------------|
| `graph_database_url` | Single string: `project_id/instance_id/database_id` |
| `project_id` | GCP project ID (required if not using `graph_database_url`) |
| `instance_id` | Spanner instance ID |
| `database_id` | Spanner database ID |
| `credentials` | Optional Google Auth credentials (default: Application Default Credentials) |

Authentication uses [Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials) unless `credentials` is provided.

## Features

- Implements Cognee's `GraphDBInterface` using Spanner Graph (GQL) and SQL DML
- Async API; blocking Spanner calls run in a thread pool
- Node and edge properties stored as JSON
- Compatible with Cognee's add/cognify/search and graph visualization

## Example

See the `examples/example.py` script for a full workflow (add data, cognify, search, graph data access). Ensure your Spanner database exists with the schema above and that ADC or credentials are set.

## License

This project is licensed under the MIT License.
