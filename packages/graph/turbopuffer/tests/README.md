# TurboPuffer graph adapter — test plan

These tests are the **specification** for the adapter (written test-first). They
mirror the existing cognee graph-adapter tests so the TurboPuffer adapter is held
to the same behavior:

| This suite | Mirrors |
|---|---|
| `test_adapter_methods.py` | `cognee/tests/integration/infrastructure/graph/test_kuzu_adapter.py` (per-method contract) |
| `test_registration.py` | `cognee-community/packages/graph/pggraph/tests/test_pggraph.py` (register + factory-shape) |
| `test_e2e_alice.py` | `cognee/tests/e2e/postgres/test_graphdb_shared.py` (full add→cognify→search) |
| `test_dataset_isolation.py` | namespace-level per-dataset isolation (vector adapter handler analog) |

## Tiers & how to run

```bash
# Unit/contract only (no network): construction, registration, signatures
pytest tests/test_registration.py

# Integration (real TurboPuffer namespace): per-method behavior + isolation
COGNEE_TURBOPUFFER_GRAPH_TESTS=1 TURBOPUFFER_API_KEY=... pytest tests/test_adapter_methods.py tests/test_dataset_isolation.py

# End-to-end (Alice in Wonderland, real LLM + TurboPuffer)
COGNEE_TURBOPUFFER_GRAPH_E2E=1 TURBOPUFFER_API_KEY=... LLM_API_KEY=... \
  pytest tests/test_e2e_alice.py
# or as a script:
python tests/test_e2e_alice.py
```

`alice_in_wonderland.txt` is resolved from `ALICE_DATA_PATH`, then a package-local
`tests/data/` copy, then the repo's `notebooks/data/` and demos copies.

## v1 scope pinned by these tests

**Implemented** (single-query or paginated-scan; no traversal):
`is_empty`, `add_node(s)`, `get_node(s)`, `delete_node(s)` (+ manual edge cascade),
`add_edge(s)`, `has_edge(s)`, `get_edges`, `get_neighbors`, `get_connections`,
`get_neighborhood(depth=1)`, `get_graph_data`, `get_filtered_graph_data`,
`get_nodeset_subgraph` (OR + client-side AND), `get_graph_metrics` (counts +
mean degree), `get_triplets_batch(offset=0)`, `delete_graph`.

**Explicitly NOT in v1** (asserted to raise `NotImplementedError`):
- `get_neighborhood(depth>=2)` — multi-hop BFS
- `query(...)` — Cypher (no query language on TurboPuffer)
- `get_triplets_batch(offset>0)` — cursor pagination only, no random offset
- connectivity-dependent metrics (`num_connected_components`, `diameter`,
  `avg_shortest_path_length`, `avg_clustering`) return the `-1` sentinel

## Stored metadata the tests assume (denormalized for single-query reads)

- **Node doc**: `id`, `name`, `type`, `belongs_to_set[]`, `degree`, `properties` (json)
- **Edge doc**: `id = "{src}__{rel}__{tgt}"`, `source_id`, `target_id`,
  `relationship_name`, **denormalized** `source_name/source_type/target_name/target_type`,
  `properties` (json)

The edge endpoint-identity denormalization is what lets `get_neighbors`/
`get_connections`/`get_edges` resolve in a single query with no hydration round-trip.
```
