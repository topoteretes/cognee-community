# Integrating future LogRocket APIs

This guide describes how to extend the existing connector when LogRocket exposes
supported retrieval, update or deletion contracts. Do not infer schemas or enable
cleanup from tool names alone. Keep the existing safe behavior until the relevant
contract and live behavior have both been verified.

## Current interface and prerequisites

The connector uses LogRocket's [official MCP server](https://docs.logrocket.com/docs/mcp)
for `find_sessions` and `find_issues`. LogRocket support confirmed in correspondence
supplied by the author on 2026-10-20 that MCP has no continuation cursor, completeness
guarantee below the limit, `updated_since` mechanism, or deletion feed/definitive lookup.
The recording/session pair is the best available session key; permanent issue identity
remains unconfirmed. The original email is not included in this package.

Before implementing new behavior, obtain documentation and sanitized, non-empty examples
covering these contracts separately for sessions and issues:

- Complete enumeration: request parameters, pagination, terminal signal, ordering or
  snapshot semantics, and what concurrent inserts/updates do to a traversal.
- Incremental updates: timestamp meaning, precision, inclusivity, tie-breakers, cursor
  expiry, late-completing sessions and updates to issues without new occurrences.
- Identity: stable IDs and behavior when issues merge, regroup or change URLs.
- Deletion: exact affected IDs and completion state, or an authoritative inventory whose
  absence semantics distinguish deletion from filtering, permissions and retention.
- Access: supported deployment/host, plan, scopes, API version, rate limits and retry rules.

Do not substitute replay downloads, warehouse adapters or destructive GDPR endpoints for
these contracts. Such changes require an explicit architectural/scope decision.

## Capture and validate the new interface

From the package directory, inspect the advertised tool schemas:

```bash
uv run --env-file .env cognee-logrocket inspect-capabilities \
  --organization YOUR_ORG --project YOUR_PROJECT \
  --output logrocket-capabilities.json --require-sync-ready
```

Exit 2 means the evidence does not establish synchronization readiness. Even candidate
schema fields do not prove runtime completeness. `tools/list` catalog pagination is
separate from session/issue data pagination.

Review the snapshot, then use a small explicit search to verify response structure:

```bash
uv run --env-file .env cognee-logrocket probe-response \
  --organization YOUR_ORG --project YOUR_PROJECT \
  --baseline tests/fixtures/live-required-tool-schemas-2026-10-16.json \
  --tool find_sessions --arguments-file examples/probe-arguments.json
```

For issues, use `--tool find_issues` and `examples/probe-issue-arguments.json`.
The baseline is a validation guard, not a claim that new schemas remain compatible.
Review actual changes before updating it. Probes allow at most five results and print
structure rather than scalar values; dynamic field names can still be sensitive.
Never commit live captures, credentials or private record values. Create synthetic
fixtures from the verified contract.

For a disposable live dataset, serve `examples/test-data/` with
`python3 -m http.server 4173 --directory examples/test-data` and open
`http://localhost:4173/?app=YOUR_ORG%2FYOUR_PROJECT`. Use only the public App ID in the
browser, never an API key. Generate synthetic custom events and captured exceptions,
then confirm the sessions/errors are visible in LogRocket before probing.

## Implementation map

| File | Change when the contract is verified |
|---|---|
| `client.py` | Add supported tool arguments, complete data pagination and update retrieval; retain schema validation and sanitized failures |
| `rest.py` | Extend only documented, verified REST routes if they are supported for connector users |
| `capabilities.py`, `probe.py`, `cli.py` | Recognize new evidence and expose reproducible read-only checks without treating schemas as runtime guarantees |
| `logrocket.py` | Normalize verified fields; add incremental state and confirmed deletion handling while preserving document-mode routing |
| `tests/` | Add synthetic contract fixtures, failure tests and real Cognee ingestion/cleanup acceptance |

Keep explicit keys, environment variables and DLT secret providers working through
`credentials.py`. MCP uses Bearer authentication; existing REST routes use `token`.
Never forward credentials to export URLs, redirects or untrusted hosts.

## Incremental state

1. Follow the verified continuation contract to its terminal signal. Reject cursor loops,
   malformed records, timeouts and incomplete traversals without committing a checkpoint.
2. Use documented ordering, timestamp boundaries and tie-breakers. A lower result count
   or an empty page alone is not proof of completion.
3. Handle late-arriving sessions with the supported overlap strategy and stable-ID merges.
   Use a separate issue update cursor when occurrence dates do not represent edits.
4. Commit DLT state only after successful loading, not after an individual page request.
   Verify rollback/retry behavior across extraction and load failures and process restarts.
5. If a native issue ID replaces the URL hash, provide an explicit migration/backfill
   strategy so existing documents are not silently duplicated or orphaned.

Do not reintroduce an unbounded per-ID content-hash dictionary in DLT state. Preserve
unchanged document identities through merge semantics and Cognee's content handling.

## Confirmed deletion and graph cleanup

Process only confirmed upstream IDs with a verified source-to-Cognee mapping and
completion semantics. A deletion request is not necessarily completed deletion.
Define handling for retention expiry, revoked access, issue merges and deselection.

The current helper enforces merge because search results are not authoritative. Do not
simply allow `replace` or assume a DLT `_deleted` column cleans up Cognee's graph. Inspect
the installed Cognee ingestion/cleanup path and prove that graph and vector records are
removed without deleting unrelated source content. Snapshot replacement is appropriate
only after a complete authoritative inventory contract is established.

Authentication failures, rate limits, partial pages, empty searches, expired cursors and
permission changes must never become inferred mass deletion.

## Existing REST checks

`LogRocketRESTClient.read_audit_logs()` follows the documented
[Audit Log API](https://docs.logrocket.com/docs/audit-log-api) `has_next`/opaque cursor
contract with budgets and loop detection. An empty terminal page with `cursor=""` was
observed live; the reader preserves the request position in that case. Audit traversal
completion does not establish exact deletion IDs or deletion completion. Counts, time
ranges and free-text descriptions must not become tombstones.

`probe-rest --resource audit --since-ms TIMESTAMP_MS --limit 2 --max-pages 5` checks that
interface; include `--organization` and `--project`. `--resource session-exports` checks
one [legacy export](https://docs.logrocket.com/docs/data-export) listing page. It does not
download files, infer end-of-stream, or advance ingestion state. The legacy service is
deprecated for SaaS; a previous project check returned HTTP 200 non-JSON and was rejected.
Confirm entitlement, deployment support and actual responses before extending it.

## Acceptance before enabling new behavior

- Test multi-page enumeration, same-timestamp boundaries, late arrivals and issue edits
  outside the current occurrence window.
- Test missing/invalid credentials, 429/5xx, schema drift, cursor cycles/expiry, malformed
  responses, page/time budgets, partial runs and failed loads. None may advance state
  or authorize deletion.
- Verify initial load, unchanged run, edited run, restart, confirmed deletion and empty
  authoritative inventory against temporary DLT staging and actual Cognee graph/vector
  cleanup. Omitted search results must continue to preserve documents.
- Run `uv run pytest -q`, lint/format, minimum-DLT tests, builds and the opt-in local
  Mistral remember/cognify/search test documented in the README.
- Perform a controlled live test in a disposable project, preserve only sanitized
  evidence, and update user-facing behavior and migration notes before release.
