# Postman connector

Ingest selected saved requests, descriptions and inherited documentation into Cognee.
Saved requests are never executed. Nested requests remain flat documents; documentation-only
collections and described folders without requests produce overview documents.

## Install and run

```sh
pip install '.[test]'
# Configure Cognee stores, extraction and embedding models separately.
export POSTMAN_API_KEY='your-key' POSTMAN_WORKSPACE_ID='your-workspace'
export POSTMAN_ACCOUNT_ID='your-account-id'
export POSTMAN_COLLECTION_UIDS='collection-uid-1,collection-uid-2'
python examples/quickstart.py
```

Create an API key in Postman Account settings → API keys. `/me` returns its account ID;
workspace settings/listing supplies the workspace ID. The account needs read access to
`/me`, `/workspaces/{id}`, collection inventory and selected trees. A selection is not a
permission boundary; the key may access other account resources. Keep keys outside Git.

For hidden-prompt setup and a dedicated local runtime instead, use
`python examples/configure_local.py`, then `python examples/postman_example.py`.
These support scripts save private credentials outside Git (directory 700, file 600),
force local SQLite/Ladybug/LanceDB and dlt paths, and inspect actual stores. They are
optional; the short quickstart uses your Cognee configuration without changing directories.
See [validation and support tooling](VALIDATION.md).

```python
from cognee_community_connector_postman import postman_source

source = postman_source(
    api_key=api_key,
    workspace_id=workspace_id,
    account_id=account_id,
    corpus_owner="my_connection",  # stable owner/connection identity
    dataset_name="postman_docs",
    resource_name="api_docs",  # stable identity; distinct for independent siblings
    collection_ids=[collection_uid],
)
await cognee.add(
    source,
    dataset_name="postman_docs",
    run_in_background=False,
    dlt_config={"write_disposition": "replace", "primary_key": "id"},
)
await cognee.cognify(datasets=["postman_docs"])
```

The factory reads no environment variables. `service=` injects a reader for hosts/tests;
`client=` remains its alias. `check_active=` is an optional synchronous authorization or
cancellation callback, checked before reads/retries and before/after snapshot publication.
Callback exceptions propagate and prevent the pending extraction from committing.
Account, owner and dataset are explicit so credential rotation cannot accidentally rebind
an existing corpus. The destination dataset must match `dataset_name`.

## Sync, ownership and deletion

`collection_ids=None` selects all visible collections, including subscriptions/forks;
`[]` explicitly empties this resource. Selection is mutable: reuse the same resource
identity when adding/removing collections, and deselected documents retire on that sync.
Account/workspace/owner/dataset/resource name bind the table, pipeline and document IDs.
Different resource names isolate sibling corpora, even with overlapping selections.
Changing those binding fields creates a new corpus; it does not retire the old one.

Collection `updatedAt`, renderer version and redaction policy control tree refetches.
All rendered names/descriptions/methods come from that tree, without external label caches.
Use `force_refresh=True` to reconcile edits with unchanged timestamps. Every run emits a
**complete retained snapshot** with `replace`, including unchanged rows and explicit empty
replacements. No-change retries replay the corpus after ingestion/cleanup failure.
Keep pipeline state persistent; concurrent runs of the same scope are unsupported.

**Default `mirror_visibility` removes previously observed collections absent from a stable,
complete listing on the next sync.** Unsharing, unsubscribing or moving out of the workspace
also counts as removal. Opt into `missing_collection_policy="require_confirmation"` to
block ambiguous disappearance until the UID is supplied in `confirmed_deleted`.
Never-observed selected UIDs, auth/API failures and incomplete/unstable inventory abort.
Explicit deselection is intentional configuration and does not require confirmation.

Two matching inventory passes converge within three attempts; fetched trees or membership
changes require a final recheck. Workspace identity/access is checked before and after.
An unchanged small workspace costs five calls including `/me`; changed/removal runs cost
six plus changed-tree reads (paging/convergence adds calls). This is bounded validation,
not an atomic provider transaction; timestamp-silent concurrent edits remain possible.
Retries wait at most two seconds each and five seconds total per extraction, with bounded
HTTP timeouts. Longer retry hints/monthly exhaustion require a later sync. API allowance
is account-specific; do not assume free-plan quotas or schedule hourly by default.

### Migrating the earlier experimental identity

Versions before this review included selection in table identity. New identities do not
silently delete those old tables. Before upgrading, retire each old scope using its old
version/identity and validated removal, or explicitly forget its **dedicated** Cognee
dataset before reingestion. Dataset-level forgetting also removes sibling corpora in that
dataset: do not use it on a shared dataset. Shared-dataset migration needs deliberate
host cleanup of the old owned scope. No broad Postman-tag cleanup or global dlt deletion
is performed. Keep old state/backups until verifying Data, graph and vector stores.

## Content, privacy and diagnostics

Unique request/folder IDs are stable across moves; missing/duplicate IDs use positional
fallback and can change on reordering. Documentation is inherited by request documents;
request-free documentation branches remain searchable as flat overview documents. Removing
the last request does not remove independently retained overview documentation.

Auth, bodies, scripts, examples and header/query/variable values are omitted. Exact auth
and sensitive-key values, explicit `redact_values` and common credential patterns are
redacted from retained prose. Ordinary values such as `service` and `application/json`
are preserved. Arbitrary secrets in prose/body samples cannot be universally detected.

`include_endpoint_metadata=True` opts into unresolved endpoint host/path templates and
header/query **names**, excluding userinfo, query values and fragments. It is off by default:
hosts, paths and even names can themselves contain private identifiers/secrets. Review
collections and use `redact_values` before opting in. Variables are never resolved.
Each row's `url` is its authenticated Postman collection API provenance URL, not an
invented public/request UI link. Only sanitized rows/cursors persist; protect local stores.

`source.cognee_sync_stats` and its resource expose integer counters, reset per extraction:
`scanned` selected collections, `skipped` reused collections, `fetched`, `documents`,
`failed` failed syncs, and `failed_auth`, `failed_rate_limit`, `failed_data` subcounts.
These describe extraction only, not committed ingestion or successful deletion cleanup.
`PostmanAuthError`, `PostmanRateLimitError` and `PostmanDataError` inherit `PostmanSyncError`;
messages exclude response bodies/credentials. Cognee can log cleanup failure without
raising from add: verify actual stores and retry after repairing the failure.

## Compatibility and evidence

Declared Python range: 3.11–3.13; tested interpreter: **3.12.13**. Dependencies deliberately
remain pinned to tested Cognee **1.6.3**, dlt **1.30.x**, httpx **0.28.1** in validation.
No broader Python/core/backend compatibility is claimed. Tree depth is bounded, but whole
response/snapshot memory is not byte-budgeted; select smaller scopes for large workspaces.
See [exact validation scope and runnable checks](VALIDATION.md).
