# cognee-community-connector-iceberg

An Apache Iceberg data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync lakehouse table schemas, partition specifications, snapshot histories, and properties
into memory — "ask my Lakehouse".

It exposes a `dlt` source you pass to `cognee.remember(...)` / `cognee.add(...)`. Iceberg
tables are rendered to markdown documents and ingested as **normal documents** (flowing
through cognee's `cognify` entity-extraction pipeline into the knowledge graph and vector
stores) via cognee's document-mode marker.

## Requirements

Requires Python `>=3.11,<=3.13` and `cognee>=1.4.0` (document-mode support via `DOCUMENT_SOURCE_ATTR`).

## Install

```bash
uv pip install cognee-community-connector-iceberg
# or from this monorepo:
cd packages/connector/iceberg && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_iceberg import iceberg_source

# Connect to any Iceberg catalog (REST, AWS Glue, Hive, Nessie)
catalog_props = {
    "type": "rest",
    "uri": "http://localhost:8181",
    "warehouse": "s3://my-lakehouse/warehouse",
}

await cognee.remember(
    iceberg_source(
        catalog_properties=catalog_props,
        namespaces=[("analytics",)],
        include_snapshots=True,
    ),
    dataset_name="iceberg_lakehouse",
)

answer = await cognee.search(
    query_text="Which tables are partitioned by day and what columns do they contain?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["iceberg_lakehouse"],
)
```

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging with
the tables currently visible in the selected namespaces on each run. Dropped or unshared
tables simply drop out of the snapshot, and cognee's `orphan_cleanup` removes them from the
knowledge graph and vector stores. Unchanged tables keep a stable content-hash `data_id`
so they are not re-ingested or re-cognified.

## Testing

```bash
uv run pytest tests/
```

The tests use in-memory mocked catalogs (`FakeIcebergCatalog`) and do not require live
external lakehouse services or cloud credentials.
