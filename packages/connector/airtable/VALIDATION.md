# Airtable implementation and local verification

Verified locally on 2026-10-08. No Git commands, commits, pushes, or PR creation were performed.
Implementation changes are confined to this package, the community README listing,
and `.github/workflows/test_airtable.yml`. Cognee core and existing connectors were not edited.

## What changed

- PAT-authenticated, selected-base/table dlt document resource; the exact returned object
  carries document-source and dataset-aware pipeline-scope markers.
- Complete source reads with incremental canonical document emission, per-table state,
  paginated comments, separately ingested schemas, and bounded/paced HTTP requests.
- Confirmed record/empty-table/whole-table deletions; deselected tables are retained.
  Any required fetch failure aborts publication, preserving existing state and staging.
- Stable emitted content omits cursor values and temporary attachment URLs. Stable
  attachment metadata and meaningful record/comment/schema changes are retained.
- Setup documentation, foreground remember/recall example, installed-wheel smoke script,
  review draft acknowledging community PR #198, and an offline CI workflow.

## Final test results

| Check | Result |
|---|---|
| Python 3.12, complete Airtable suite | **107 passed**, 0 failed, 0 errors, 0 skipped |
| Python 3.11, Airtable unit/staging/example suite | **97 passed**, 0 failed, 0 errors, 0 skipped |
| Existing Confluence/Gmail/Slack/Drive source and staging tests | **52 passed, 1 skipped** |
| Targeted core document routing/cleanup/replay/state tests | **33 passed** |
| Ruff lint and format check | Passed |
| actionlint 1.7.12, new workflow | Passed |
| Wheel and source distribution build | Passed |
| Installed wheel outside the checkout, real document ingestion | Passed |
| Hosted GitHub Actions | Not run; changes remain local |
| Live Airtable and real model quality | Not run; local HTTP/models are deterministic substitutes |

The existing regression skip is Confluence's optional DuckDB destination test. The
52 passes plus 33 core passes and one skip reproduce the earlier targeted baseline.
Existing Notion tests were not run: attempts to prepare optional Notion/Google/DuckDB
dependencies stalled and were stopped or timed out. These installs were outside the
frozen Airtable dependency set; no required Airtable test was skipped.

The integration suite keeps real SQLite metadata/staging, Ladybug graphs, LanceDB vectors,
ingestion, retrieval, indexing, and cleanup. It proves changed facts replace old facts,
deleted facts disappear from graph/vectors/retrieval, and shared/surviving facts remain.
It covers base/table/dataset isolation and downstream recovery, including empty retained
staging and a fresh-interpreter retry after staging committed but Data storage failed.
Only HTTP/model boundaries are substituted; injected backend faults exercise real recovery.

Cognee 1.6.3 treats orphan cleanup as best-effort: a backend deletion failure can be logged
while `remember()` returns completed. Tests assert stale content remains during that injected
failure and is purged by an unchanged retry. This is documented rather than hidden.

## Reproduce the package gates

```bash
cd /home/linux/Desktop/cognee-community/packages/connector/airtable
uv sync --frozen --all-extras --python 3.12
uv run --frozen pytest tests -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv build
```

The same tests/unit tree was run using a separate Python 3.11.11 environment with the
exported locked runtime constraints, pytest 8.4.2, and pytest-asyncio 1.4.0.
CI enforces nonempty test execution and zero skipped required tests via JUnit reports.

## Dependency and packaging provenance

Python 3.12.15 imported Cognee 1.6.3 and dlt 1.30.0 from
`packages/connector/airtable/.venv/lib64/python3.12/site-packages/`, not the sibling
Cognee source checkout. The core regression runner imported this same installed release
before collecting the sibling repository's four targeted test files.

The installed-wheel smoke used `/tmp/airtable-4766-wheel/lib64/python3.12/site-packages/`
for Cognee, dlt, and this connector, with an external temporary working directory,
fresh temporary storage and dlt state, and no PYTHONPATH override. The installed module,
built wheel, and final source module bytes were checked to match. The smoke read the
actual persisted Data content through Cognee's file reader and asserted its fixture fact.

JUnit reports from this run are `/tmp/airtable-4766-py312-results.xml` and
`/tmp/airtable-4766-py311-results.xml`; installed-wheel output is
`/tmp/airtable-4766-wheel-smoke.log`. These temporary files are local evidence, not package data.

For wheel-smoke commands and optional live acceptance, see README.md. For concise review
text acknowledging prior work, see REVIEW.md. Local fixture tests establish connector and
local storage behavior; they do not establish live API permissions, model answer quality,
hosted CI success, or every Cognee backend/platform.
