# Cognee Community Workflows

This directory contains GitHub Actions workflows for testing community adapters
against the pinned cognee version (see each package's `pyproject.toml`,
currently `cognee==1.4.1`).

## Test tiers

Every graph/vector/hybrid adapter package structures its tests as:

- **unit** (`tests/unit/`) — offline contract tests plus any mocked/pure-logic
  suites. No services, no secrets. Runs for **all 23 adapter packages** on
  every PR via `adapter_contract_tests.yml` (see
  `packages/shared/contract_suite/README.md`). This is the always-on signal,
  including for adapters whose backing service is cloud-only (Pinecone, Moss,
  TurboPuffer, Spanner, Azure AI Search).
- **integration/e2e** (`tests/`) — run against a dockerized service and/or the
  full `add → cognify → search` pipeline. These need LLM/EMBEDDING secrets and
  run via the per-adapter workflows below (path-filtered on PRs, fanned out by
  the suite on push/dispatch).

## Workflow files

### Main orchestration
- `community_test_suite.yml` — main workflow that runs everything: contract
  tests + vector/graph fan-outs + pipelines/retrievers/tasks. Triggers on push
  to `main`/`dev`, `repository_dispatch: new-main-release`, and manual dispatch.
- `adapter_contract_tests.yml` — 23-package matrix running `pytest tests/unit`
  per package. No secrets needed; safe on fork PRs.
- `vector_db_community_tests.yml` — reusable fan-out for vector adapter tests
- `graph_db_community_tests.yml` — reusable fan-out for graph adapter tests
- `community_pipeline_tests.yml`, `community_retriever_tests.yml`,
  `community_task_tests.yml` — non-adapter package tests

### Individual adapter tests (docker service or hosted instance)
- `test_qdrant.yml`, `test_redis.yml`, `test_valkey.yml`, `test_milvus.yml`,
  `test_opensearch.yml`, `test_singlestore.yml`, `test_weaviate.yml` (hosted,
  needs `WEAVIATE_API_URL`/`WEAVIATE_API_KEY`) — vector
- `test_memgraph.yml`, `test_falkordb.yml`, `test_turingdb.yml`,
  `test_pggraph.yml` — graph (NetworkX runs inline in
  `graph_db_community_tests.yml`; it needs no server)
- `test_duckdb.yml` — hybrid (in-process, no service)
- `test_codify.yml` — codify pipeline

### Adapters covered by contract tests only
azureaisearch, moss, pinecone, turbopuffer (graph + vector), spanner,
arcadedb (graph + hybrid), opengauss, helixdb — their backing services are
cloud-only or have no reliable CI docker story yet. Their unit tier (contract
conformance + any mocked suites, e.g. openGauss's fully-mocked adapter tests
and Spanner's mocked suite) runs on every PR; live tests are documented in
each package's README for manual runs.

### Cognee dev-version testing
- `test_with_cognee_dev.yml` — manual: test one package against cognee@dev
- `test_all_with_cognee_dev.yml` — manual: matrix over packages against an
  arbitrary cognee ref

## Usage

Tests run automatically on push/PR to `main` or `dev`. To manually trigger all
tests: Actions → "Community Test Suite" → Run workflow (databases: "all").
Individual adapter workflows also trigger automatically when files in their
package directory change, and can be dispatched manually.
