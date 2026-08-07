# contract_suite — shared offline conformance checks for community adapters

This directory holds **non-packaged** test helpers shared by every adapter
package under `packages/graph/`, `packages/vector/`, and `packages/hybrid/`.
It is not published to PyPI and has no dependencies beyond `cognee` itself
(which every adapter package already depends on).

## What it provides

- `FakeEmbeddingEngine` — a deterministic, network-free implementation of
  cognee's `EmbeddingEngine` protocol. Use it in unit and integration tests so
  no LLM or embedding API keys are ever needed.
- `assert_vector_contract(AdapterCls)` — asserts a vector adapter satisfies the
  cognee 1.4.1 call surface: real `VectorDBInterface` subclassing, the factory
  construction shape (`url=`, `api_key=`, `embedding_engine=`,
  `database_name=`), and the exact `search` / `batch_search` /
  `create_vector_index` / `index_data_points` call shapes cognee core uses.
- `assert_graph_contract(AdapterCls)` — same for graph adapters, including the
  1.4.1 hard requirement that `add_nodes` / `add_edges` accept
  `source_ref_key=` and `pipeline_run_id=` keywords, and that
  `get_nodeset_subgraph` accepts `node_name_filter_operator=`.

## How adapter packages use it

Each package's `tests/conftest.py` adds this directory's parent to
`sys.path`:

```python
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "shared"))
```

and its unit tier contains a `tests/unit/test_contract.py` along the lines of:

```python
from contract_suite import assert_vector_contract
from cognee_community_vector_adapter_example.example_adapter import ExampleAdapter


def test_conforms_to_cognee_141_vector_contract():
    assert_vector_contract(ExampleAdapter)
```

Hybrid adapters assert **both** contracts.

These contract tests are the always-on CI signal for adapters whose backing
service is cloud-only (no docker image, needs paid credentials): they prove
signature-level compatibility with the pinned cognee version on every PR
without any secrets.

## Test tiers

Every adapter package structures its tests as:

- `tests/unit/` — no network, no services, no secrets. Contract test plus any
  mocked/pure-logic tests. Runs on every PR for every package via
  `.github/workflows/adapter_contract_tests.yml`.
- `tests/integration/` — talks to a real database (dockerized in CI) but uses
  `FakeEmbeddingEngine`; no LLM/embedding secrets.
- `tests/e2e/` — full `cognee.add → cognify → search` pipeline; needs
  LLM/embedding secrets and runs only where secrets are available.
