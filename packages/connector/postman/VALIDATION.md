# Validation and support tooling

## Local checks

From this package directory:

```sh
RUNTIME__DLTHUB_TELEMETRY=false python -m pytest tests -q
ruff check .
ruff format --check .
uv build --wheel
```

The clean validation environment uses Python 3.12.13, released Cognee 1.6.3,
dlt 1.30.0 and httpx 0.28.1. Install the built wheel before validating it; source
checkout tests alone do not prove the installed artifact. The core capability guard
requires document-sync support. Python 3.10, other minor Python versions, newer Cognee,
and other dlt ranges are not validated by these receipts; pins remain deliberate.

The offline suite uses fake provider responses. It does exercise real dlt/SQLite
replacement, Cognee staging read-back, interrupted-load recovery, source namespace
isolation, mutable selection/sibling tables, docs-only documents, cancellation and
safe rendering. A separate process regression creates real cached state and pending
load packages in runtime A, verifies runtime B is empty and returns to unchanged A.
It injects synthetic global configuration without changing the user's global files.

## Real Cognee stores with a fake provider

```sh
# Configure local extraction/embedding models first. Run in its own process.
POSTMAN_MODEL_TEST=1 python -m pytest tests/test_cognee_scope_lifecycle.py -q
```

This optional test populates two source scopes in one dataset, changes one selection
to empty, verifies that its Data/graph/vectors disappear while the sibling remains
searchable, repeats the empty sync, then deletes the final upstream collection under
the default policy. The provider is fake; stores and model inference are real.

## Authenticated synthetic lifecycle

```sh
python examples/configure_local.py
python examples/postman_lifecycle.py --allow-provider-writes
```

Use a dedicated test workspace. The hidden-prompt setup saves configuration to
`~/.config/cognee-postman-4711/credentials.json` and refuses redirects/overwrites.
The lifecycle creates only its own two synthetic collections; existing collections
are not selected or modified. It checks initial/unchanged sync, selection removal and
restoration, edits, a docs-only collection, final-request deletion/restoration,
whole-collection next-sync deletion with sibling preservation, final removal and empty
replay. Its fixture cleanup runs in `finally`; retain logs and inspect the test workspace
if provider access fails. No saved request is executed.

It prints a fresh runtime directory. Do not reuse a lifecycle directory. The support
runtime forces local SQLite/Ladybug/LanceDB plus dlt state/pending loads before imports,
clears inherited backend/dlt overrides and changes working directory. This deliberate
isolation is for support tooling, not a factory side effect or arbitrary embedded runtime
reset. Old global state is neither migrated nor deleted.

```sh
POSTMAN_RUNTIME_DIR='/path/printed/by/lifecycle' \
  python examples/verify_stores.py --dataset postman_4711_lifecycle --expect-empty
```

The verifier imports no Cognee test helpers and checks stored Data files, actual graph
properties and every LanceDB table's rows/payloads. Unsupported backend inspection must
not be interpreted as an empty store. `postman_example.py` also accepts
`POSTMAN_FORBIDDEN_TEXT` and `POSTMAN_EXPECT_EMPTY=1` to assert stale-content absence.

## Evidence boundaries

The 12 October baseline passed 70 offline tests, a separate local-model scope test,
Ruff and wheel build. Earlier real-account installed-wheel lifecycles passed add/search,
updates, request/collection deletion, sibling preservation and all-store empty replay.
The 19 October review follow-up adds regressions and reruns the updated artifact; its
exact results are recorded in the PR validation section. Do not treat the optional test's
default skip as a model pass, or fake-provider checks as real-account checks.

Cached GLiNER2.5/bge-small models were used without downloads or paid model calls. Earlier
fault injection exercised ingestion/cleanup failures with unchanged retry recovery;
it does not cover every partial transaction/crash. Tested fixtures are small. Provider
snapshots are nontransactional, arbitrary prose-secret removal is heuristic, concurrent
same-scope extraction is unsupported and whole-response memory remains unbudgeted.
