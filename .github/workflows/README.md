# Cognee Community Workflows

This directory contains GitHub Actions workflows for testing community adapters
against the pinned cognee version (see each package's `pyproject.toml`,
currently `cognee==1.6.1`).

## Test tiers

Every graph/vector/hybrid adapter package structures its tests as:

- **unit** (`tests/unit/`) — offline contract tests plus any mocked/pure-logic
  suites. No services, no secrets. Runs for **all 24 adapter packages** on
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
- `connector_package_validation.yml` — pull-request matrix that discovers changed
  connector packages and runs their tests, Ruff lint/format checks, Python
  compilation, and wheel build without external credentials.
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

### Cognee version bump
- `bump_cognee.yml` — daily and manual (optional `version` input). When a new
  stable cognee is on PyPI, opens one PR on `automation/bump-cognee-<base>` that bumps
  every adapter under `packages/{graph,vector,hybrid}`:
  - pins, adapter package versions, version strings and lock files, done by
    `.github/scripts/bump_cognee.py`
  - new optional adapter methods, done by an agent step following
    `.github/prompts/bump_cognee_agent.md`

  A person reviews the PR, fixes breaking changes on its branch, merges and
  publishes to PyPI. The workflow does nothing when the adapters are up to date
  or a PR for that version is open, and never force-pushes over a reviewer's
  commits; a branch left behind by a merged or closed bump PR is rebuilt. Closing a bump PR skips that version on the daily runs; a manual run
  bumps it anyway. Needs the `COGNEE_BUMP_PR_TOKEN` secret (a PAT or app token, since
  PRs opened with `GITHUB_TOKEN` don't run CI) and `ANTHROPIC_API_KEY` for the
  agent step. Run the mechanical part locally with
  `python .github/scripts/bump_cognee.py apply --version X --skip-locks`.

## Usage

Tests run automatically on push/PR to `main` or `dev`. To manually trigger all
tests: Actions → "Community Test Suite" → Run workflow (databases: "all").
Individual adapter workflows also trigger automatically when files in their
package directory change, and can be dispatched manually.
