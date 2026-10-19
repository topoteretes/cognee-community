# Postman connector

Sync selected requests and documentation into Cognee without executing saved requests.
Nested requests become flat documents. Described request-free collections/folders also
produce overview documents; populated branches retain inherited request documentation.

## Install and run

```sh
pip install '.[test]'
# Configure your Cognee stores and extraction/embedding models separately.
export POSTMAN_API_KEY='your-key' POSTMAN_WORKSPACE_ID='your-workspace'
export POSTMAN_COLLECTION_UIDS='collection-uid-1,collection-uid-2'
python examples/quickstart.py
```

Generate a user API key in Postman Account settings → API keys. Use the workspace ID
from its settings/listing. Keep credentials outside Git and shell history. The account
needs read access to `/me`, `/workspaces/{id}`, collection inventory and selected trees.
Selections are not permission boundaries: the key may access other account resources.

```python
source = postman_source(
    api_key=api_key,
    workspace_id=workspace_id,
    resource_name="api_docs",  # required: stable, distinct identity for each connection
    collection_ids=[collection_uid],
)
await cognee.remember(
    source,
    dataset_name="postman_docs",
    run_in_background=False,
    dlt_config={"write_disposition": "replace", "primary_key": "id"},
)
```

`resource_name` is required so independent connections cannot silently share the default
table. Keep it fixed when editing selection; choose different names for sibling sources.
The factory verifies `/me` **during construction**, resolves the account before naming
its source/table, and stores that identity for both minimal and explicit-account calls.
An optional `account_id` must match. Optional owner/dataset constraints remain available
for advanced hosts. The factory reads no environment variables and changes no backend
paths. `service=` injects a reader; `client=` remains an alias.

## Incremental sync and deletion

`collection_ids=None` selects all visible collections, including subscriptions/forks;
`[]` intentionally empties this resource. Changing selection replaces that resource's
snapshot and retires deselected content without touching differently named siblings.
Account/workspace/owner/dataset/resource identity binds document IDs, table and state;
changing those binding fields creates another corpus.

Collection `updatedAt`, renderer version and redaction policy control tree fetching.
Use `force_refresh=True` for timestamp-silent edits. Every extraction emits the **complete
retained snapshot** with `replace`, including cached rows and explicit empty replacements.
Keep pipeline state persistent. Same-scope concurrent extraction is unsupported.

Default `mirror_visibility` removes a previously observed missing collection on the
next sync. Unsharing, moving or permission loss can also cause absence. **Bulk protection:**
more than one unconfirmed missing collection aborts instead of publishing removals.
Pass missing UIDs in `confirmed_deleted` after checking the workspace, or explicitly set
`max_unconfirmed_removals`. Setting it to zero requires confirmation for every disappearance.
The default still permits final-single-collection deletion automatically; it cannot prove
whether one missing collection was physically deleted or became invisible. Explicit
selection edits are intentional and bypass the provider-disappearance budget.

Use `missing_collection_policy="require_confirmation"` for conservative handling of any
visibility loss. Unknown selected UIDs, inaccessible workspace, API/quota faults and
malformed/incomplete pagination abort. Confirmations cannot remove visible collections.

An unchanged small workspace costs **three reads**: `/me` at construction, workspace
validation and one paginated inventory. Fetches or membership changes require a second
matching inventory before publication. This reduces quota use while retaining checks on
changed snapshots; it is not an atomic provider transaction. HTTP retries have finite
attempts/timeouts and at most two seconds per sleep/five seconds per client phase.
Longer retry hints/monthly exhaustion require a later sync. Allowance is account-specific.

## Content and privacy

Endpoint templates and header/query names are included by default, with variables left
unresolved and userinfo/query values/fragments removed. `include_endpoint_metadata=False`
disables them. Known credential patterns in paths are redacted too. Request rows with
valid Postman item UUIDs link to the request UI; missing/duplicate IDs fall back to the
collection UI. Positional identity fallback changes on reorder/move.

Auth, bodies, scripts, examples and raw header/query/variable values are omitted. Redaction
recognizes secret-typed variables, sensitive names, auth-declared headers, credential-like
query values, common token patterns and explicit `redact_values`. Short secrets use token
boundaries so ordinary words, counts and versions are not corrupted. This is best-effort
filtering, not universal secret detection: arbitrary prose, hosts, paths or unusual names
can contain undiscoverable private values. Review documentation and use explicit values.
Raw trees are not persisted; sanitized rows/cursors remain in local state and stores.

## Host hooks and diagnostics

`check_active=` is a synchronous host authorization/cancellation callback. A single
coordinator checks it before every HTTP attempt, including retries; publication has
separate before/after checkpoints. Injected PostmanClient hooks are retained and identical
callbacks are deduplicated. Callback exceptions abort; factory preflight errors raise
before a source is returned.

`source.cognee_sync_stats` and its resource share integer extraction counters: `scanned`
selected collections, `skipped` reused, `fetched`, `documents`, `failed`, and typed
`failed_auth`/`failed_rate_limit`/`failed_data`. All extraction exceptions increment failed;
a static Cognee warning omits private exception text. Typed errors inherit PostmanSyncError.
Counters are not proof of committed ingestion or successful cleanup. Cognee can log orphan
cleanup failure without raising: inspect actual Data/graph/vector stores and retry.

## Compatibility, migration and verification

Declared Python: **>=3.10,<3.15**, matching core; tested versions and exact dependency
combinations/results are in [VALIDATION_RESULTS.json](VALIDATION_RESULTS.json).
Dependencies: Cognee>=1.6.3,<2 and dlt>=1.29,<1.31. Current core also rejects dlt1.31;
future allowed releases and unlisted backends are not individually validated.

**Experimental upgrade:** earlier selection-based, default-name and unresolved-account
identities may leave old tables/documents. Retire old owned scopes with the old version
or deliberately forget a dedicated old dataset before reingesting. Dataset-level cleanup
also deletes siblings: do not use it on shared datasets. No broad automatic cleanup occurs.
See [CHANGELOG.md](CHANGELOG.md).

The only public example is quickstart.py. Credential-writing/blog scripts are excluded.
Optional isolated lifecycle/store inspection is test support under `tests/support/`,
not imported by the connector or quickstart. [VALIDATION.md](VALIDATION.md) gives commands,
model configuration and evidence boundaries. Depth is bounded; response/snapshot memory
is not byte-budgeted. Select smaller scopes for large workspaces.
