# Reproducing validation

[VALIDATION_RESULTS.json](VALIDATION_RESULTS.json) contains the recorded environment
versions, exact pytest summaries, tested module SHA-256 and authenticated per-step store
counts. These are execution receipts, not proof of every environment or provider state.
The complete test sources and synthetic lifecycle are in this repository. Historical
counts in older reviews are superseded by the versioned results file.

## Installed-wheel offline suite

From this package directory, with uv installed:

```sh
uv build --wheel --out-dir /tmp/postman-wheel
uv venv --python 3.10 /tmp/postman-check
uv pip install --python /tmp/postman-check/bin/python \
  'cognee==1.6.3' 'dlt[sqlalchemy]==1.29.0' 'httpx==0.28.1' 'pytest>=8,<10' \
  /tmp/postman-wheel/cognee_community_connector_postman-0.1.0-py3-none-any.whl
COGNEE_LOG_FILE=false TELEMETRY_DISABLED=1 RUNTIME__DLTHUB_TELEMETRY=false \
  /tmp/postman-check/bin/python -m pytest tests -q
ruff check .
ruff format --check .
```

Use a fresh environment for each matrix row in the results file. The other recorded
combinations use Python3.12/Cognee1.6.3/dlt1.30.0 and Python3.14/Cognee1.6.5/dlt1.30.0.
The declared interpreter range is **>=3.10,<3.15**, matching core. Allowed dependencies
are Cognee>=1.6.3,<2 and dlt>=1.29,<1.31; the current core also excludes dlt1.31.
Future versions within those ranges are not individually prevalidated.

Offline means fake Postman responses, not fake dlt storage: tests use actual SQLite
replacement, source cache, pending-load recovery, Cognee staging, sibling resources and
empty snapshots. Tests exercise factory defaults, required resource names, account
preflight identity, bulk-removal confirmation, redaction, typed and unexpected failures,
authorization checkpoints and the three-call unchanged path. A model test is skipped by
default; that skip must not be reported as a model-integration pass.

## Real Cognee stores and local models

Configure extraction/embedding models first. The recorded model run used cached
GLiNER2.5 and bge-small with Cognee1.6.3; that setup requires its optional extra:

```sh
pip install 'cognee[gliner]==1.6.3'
export GRAPH_EXTRACTOR=gliner_demo GLINER_INFERENCE_THREADS=1 GLINER_AUTO_INSTALL=false
# Point HF_HOME and FASTEMBED_CACHE_PATH at your populated model caches.
# Use HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1 to prevent model downloads.
POSTMAN_MODEL_TEST=1 python -m pytest tests/test_cognee_scope_lifecycle.py -q
```

Run in its own process. This uses `remember` with a fake provider and real
SQLite/Ladybug/LanceDB stores: two resources populate one dataset; deselecting one
preserves the sibling's Data/graph/vector/search content; final default deletion empties
stores. Other models/backends require separate configuration and validation.

## Authenticated synthetic lifecycle (optional)

Only quickstart.py is a public example. The helpers below are test support, deliberately
isolated from caller stores. They do not run from the connector or quickstart. They set
local backend/dlt paths and change directory before SDK imports; they are not an API for
resetting an already initialized SDK process. No script writes credentials.

Create a private JSON file outside Git with POSTMAN_API_KEY, POSTMAN_WORKSPACE_ID and
POSTMAN_ACCOUNT_ID. Use a dedicated test workspace and file permissions600. Then:

```sh
POSTMAN_TEST_CONFIG='/private/path/test-postman.json' \
  python tests/support/postman_lifecycle.py --allow-provider-writes
```

This creates/deletes only its own two synthetic collections, never executes requests,
and runs eleven checks: initial/unchanged, visible selection removal/restoration, edit,
docs-only collection, final-request deletion/restoration, whole-collection removal with
sibling preservation, final collection removal and empty replay. Fixture cleanup runs
in `finally`; verify the workspace if provider errors prevent cleanup. It prints a fresh
local runtime directory and retains local evidence; do not reuse that directory.

```sh
POSTMAN_RUNTIME_DIR='/runtime/printed/by/lifecycle' \
  python tests/support/verify_stores.py --dataset postman_4711_lifecycle --expect-empty
```

The verifier reads actual Data files, graph properties and every LanceDB table's payloads,
without importing Cognee test helpers. It supports these pinned local backends only;
unsupported inspection is not an empty-storage receipt. Core can warn about cleanup
without raising, so search/exit code alone is not a forgetting proof.

## Limits

Filtering is best effort, including secret-typed variables, declared/raw custom auth
headers, known token patterns and short-value boundaries. Arbitrary secrets cannot be
universally identified. A stable partial provider listing may still mean visibility
loss; the bulk guard limits automatic removals but does not prove physical deletion.
Final-single-collection automatic removal is a deliberate next-sync acceptance choice.

No atomic provider snapshot, same-scope concurrent ingestion, full backend/Python matrix,
every partial crash or byte-budgeted large response is claimed. Exact environment and
live-account receipts apply to small synthetic fixtures and the listed versions only.
