# Review draft

Implements the Airtable data source requested in [cognee#4766](https://github.com/topoteretes/cognee/issues/4766):
PAT authentication, selected tables, records/schema/comments, changed documents, and
confirmed upstream deletions through Cognee's document-ingestion path.

This overlaps with [community PR #198](https://github.com/topoteretes/cognee-community/pull/198).
Its published design describes timestamp-gated updates, a guard that preserves records
after an empty sweep, optional metadata scope, and best-effort comments. This implementation
reconciles complete source inventories, propagates confirmed empty/whole-table deletion,
requires metadata, and aborts publication if required comment enrichment fails. Canonical
staged content prevents churn from cursor-only changes and expiring attachment URLs.

Local verification: 107 tests passed on Python 3.12 (97 unit/staging/example tests and
10 real-storage integration tests), with no failures or skips. The same 97 unit tests
passed on Python 3.11. Tests use released Cognee 1.6.3 and locked dlt 1.30.0, real
SQLite/Ladybug/LanceDB, deterministic model substitutes, and blocked live HTTP.
The built wheel was installed outside the checkout and ingested a fixture successfully.
Ruff lint/format and actionlint passed. Targeted existing connector/core regressions:
85 passed, one optional DuckDB test skipped, matching the earlier baseline.

The offline workflow runs the connector tests and packaging checks on pull requests.
Hosted CI and live Airtable validation have not been performed. Detailed commands,
coverage, provenance, and the optional dependency-install limitation are in VALIDATION.md.
