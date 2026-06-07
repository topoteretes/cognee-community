# cognee-community-graph-adapter-turbopuffer

A community **graph** database adapter for [cognee](https://github.com/topoteretes/cognee)
backed by [TurboPuffer](https://turbopuffer.com).

TurboPuffer is a vector/document store with no native graph traversal or query
language, so this adapter **emulates a graph** on two TurboPuffer namespaces per
dataset — exactly as the core Postgres graph adapter emulates one on relational
tables. All traversal is done client-side with single-hop edge queries.

## Storage model

Per dataset, namespace-prefixed by `database_name`:

- `{database_name}_graph_node` — one doc per node: `id`, `name`, `type`
  (the DataPoint class name), `belongs_to_set` (string[]), and a non-filterable
  `properties` JSON blob holding all remaining fields (so large `text` is kept
  in full).
- `{database_name}_graph_edge` — one doc per edge: a deterministic `uuid5` id
  (TurboPuffer caps ids at 64 bytes), `source_id`, `target_id`,
  `relationship_name`, denormalized endpoint identity
  (`source_name`/`source_type`/`target_name`/`target_type`), and the full edge
  `properties` JSON blob.

## Install

```bash
uv pip install -e cognee-community/packages/graph/turbopuffer
# or, from the package directory:
uv pip install -e .
```

## Configure & use

```python
import cognee
from cognee_community_graph_adapter_turbopuffer import register

register()  # registers the "turbopuffer" graph provider + dataset handler
cognee.config.set_graph_database_provider("turbopuffer")

# Environment:
#   TURBOPUFFER_API_KEY   (required)
#   TURBOPUFFER_REGION    (required by the SDK; e.g. gcp-us-central1)

await cognee.add("Alice followed the White Rabbit.")
await cognee.cognify()
results = await cognee.search("Who does Alice meet?")
```

For multi-tenant **backend access control**, also select the graph dataset
handler:

```bash
GRAPH_DATABASE_PROVIDER=turbopuffer
GRAPH_DATASET_DATABASE_HANDLER=turbopuffer_graph
```

(The handler is registered under `turbopuffer_graph` so it does not collide with
the TurboPuffer *vector* adapter's `turbopuffer` handler.)

## Supported vs. not (v1)

**Supported:** CRUD, single-hop reads (`get_neighbors`, `get_connections`,
`get_edges`), `get_neighborhood(depth=1)`, `get_graph_data`,
`get_filtered_graph_data`, `get_nodeset_subgraph` (OR + client-side AND),
`get_graph_metrics` (counts + mean degree), `get_triplets_batch` (offset
pagination via deterministic cursor scan), `remove_belongs_to_set_tags`,
`delete_graph`, per-dataset isolation.

**Not in v1:** multi-hop BFS (`get_neighborhood(depth>=2)`) and raw Cypher
(`query`) raise `NotImplementedError`. Connectivity-dependent metrics
(connected components, diameter, clustering) return the `-1` sentinel, matching
the Postgres adapter.

## Examples & tests

```bash
# build the Alice graph and query it
GRAPH_DATABASE_PROVIDER=turbopuffer python examples/example.py
python examples/inspect_graph.py --raw       # inspect node/edge namespaces
python examples/export_graph.py               # dump nodes.json / edges.json

# tests (see tests/README.md for tiers)
./run_tests.sh integration                    # live per-method + isolation
./run_tests.sh e2e                            # full add->cognify->search on Alice
```
