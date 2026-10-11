# Postman connector

Sync selected Postman collections as one Cognee document per request, including
collection/folder documentation, method and breadcrumb. Saved requests are never executed.

## Install and connect

Python 3.11–3.13; the demonstrated clean installation used Python 3.12.13,
Cognee 1.6.3 and dlt 1.30.0. From this package directory:

```sh
pip install '.[test]'
python examples/configure_local.py
```

Create a dedicated Postman test workspace. In Postman Account settings → API keys,
generate a user API key. The helper accepts it through a hidden terminal prompt,
verifies `/me`, lists workspaces and validates the selected workspace. It stores
credentials outside Git in `~/.config/cognee-postman-4711/credentials.json`, with
file permissions 600 and directory permissions 700. It blocks redirects and refuses
to overwrite existing credentials. Never paste credentials into a chat, issue or PR.

Read access is needed to `/me`, `/workspaces/{id}`, collection inventory and selected
collection trees. A user API key can access other resources available to that account;
selection is not a permission boundary. Revoke the test key when finished.

## Run with Cognee

Configure Cognee's extraction and embedding models separately. The connector does
not download models; Cognee may download them on first use. The demonstration used
cached `fastino/gliner2.5-base-v1` and `BAAI/bge-small-en-v1.5`, with
`GRAPH_EXTRACTOR=gliner_demo`, `GLINER_INFERENCE_THREADS=1` and offline model caches.
That setup requires `pip install 'cognee[gliner]==1.6.3'`. Chunk search needs no chat LLM.

```sh
export POSTMAN_COLLECTION_UIDS='your-selected-collection-uid'
python examples/postman_example.py
```

The example forces dedicated local SQLite/Ladybug/LanceDB stores under
`~/.local/share/cognee-postman-4711`; `POSTMAN_RUNTIME_DIR` can choose a dedicated
alternative. Inherited database paths/providers are overridden. Model configuration
is retained. The same runtime and collection selection must be used for reruns.
It performs foreground add, cognify and chunk search, then inspects the actual stores.
Set `POSTMAN_FORBIDDEN_TEXT='obsolete fixture text'` to assert absence across Data,
graph properties and vector payloads, or `POSTMAN_EXPECT_EMPTY=1` to assert zero rows,
graph nodes/edges and vector rows. These checks raise on failure.

The Python source API also supports your own Cognee dataset/backend configuration:

```python
from cognee_community_connector_postman import postman_source

source = postman_source(
    api_key=api_key,
    workspace_id=workspace_id,
    account_id=account_id,
    corpus_owner="my_postman_corpus",
    dataset_name="postman_docs",
    collection_ids=[collection_uid],
    missing_collection_policy="mirror_visibility",
)
await cognee.add(
    source,
    dataset_name="postman_docs",
    run_in_background=False,
    dlt_config={"write_disposition": "replace", "primary_key": "id"},
)
await cognee.cognify(datasets=["postman_docs"])
```

`collection_ids=None` selects all visible workspace collections, including subscriptions
and forks; `[]` selects none. The example requires explicit nonempty selection.

## Incremental sync and deletion

Changed collection trees are fetched by `updatedAt`; unchanged sanitized documents
are reused from dlt source state. Every run emits the **complete retained corpus**
with `replace`. Sending only changed documents would erase unchanged staging rows.
Explicit columns produce a zero-row replacement after the final request disappears.
No-change reruns replay the full corpus after downstream ingestion/cleanup failures.
Keep dlt's pipeline directory persistent; do not run the same namespace concurrently.

Inventory must have valid paging metadata, unique UIDs, complete totals and cursors.
Two consecutive equal inventories converge within three attempts. Workspace access
is checked before/after and inventory is rechecked after tree fetching. API errors,
quota exhaustion, malformed trees or unstable/incomplete inventory abort before
publishing rows. This validation is bounded, not a provider transaction.

Deleting a request within a surviving collection removes it automatically. For whole
collections there are two explicit policies:

- `mirror_visibility`: remove previously observed collections absent from a complete,
  stable, validated listing on the next sync. **Unsharing, unsubscribing or moving out
  of the workspace also removes their local documents.** Unknown, never-observed
  selected UIDs still fail instead of silently producing an empty corpus. This policy
  is used by both runnable examples for automatic forget-on-delete.
- `require_confirmation` (source API default): abort if an observed collection disappears
  until its UID is passed in `confirmed_deleted`. This preserves records when visibility
  loss is ambiguous. The example's optional `POSTMAN_CONFIRMED_DELETED_UIDS` supplies
  explicit confirmation. A visible collection cannot be confirmed removed.

Acknowledged removals persist across reruns; reappearance refetches the collection and
clears its acknowledgement. Account, workspace, corpus owner, dataset and selection
namespace document IDs, table names and pipeline state. Rotating keys preserves identity.
Changing selection/owner/dataset creates a new corpus and does not retire the old one.
To retire the old scope, keep its original namespace/selection, remove or unshare its
selected collections and sync using the matching policy; alternatively explicitly
forget the old dedicated dataset through Cognee before migrating. Dataset-level forgetting
also removes other corpora sharing that dataset, so use separate datasets for migration.

Cognee can log orphan cleanup failures without raising from add. Verify actual stores
when checking forgetting; exit code/search alone is insufficient. Full replay allows a
retry after cleanup is repaired.

## Reproducible lifecycle and store checks

```sh
# Requires a dedicated workspace and explicitly authorizes only synthetic fixture writes.
python examples/postman_lifecycle.py --allow-provider-writes
```

The lifecycle creates two synthetic collections and checks ingest/search, unchanged
sync, edits, final-request removal, restoration, nonempty whole-collection removal
while a sibling survives, final collection removal and empty replay. It inspects Data,
actual graph properties and all LanceDB table rows/payloads. Its own provider fixtures
are removed in `finally`; existing collections are never selected or modified.
By default each lifecycle run creates a fresh local runtime and prints its path.
If setting `POSTMAN_RUNTIME_DIR`, use a fresh directory for each run. Failed runs leave
local evidence for diagnosis and are not automatically pruned.

The verifier performs no Postman calls and imports no Cognee test helpers:

```sh
POSTMAN_RUNTIME_DIR='/path/printed/by/lifecycle' \
  python examples/verify_stores.py --dataset postman_4711_lifecycle --expect-empty
python examples/verify_stores.py --forbid-text 'obsolete fixture text'
```

It supports the example's pinned local backends. Other backends require their own
store inspection; unsupported inspection must not be interpreted as empty storage.

## Privacy, identity and quotas

Unique item IDs remain stable across moves/renames; missing or duplicate IDs use
positional fallback, which changes on reorder/move. Inherited documentation changes
refresh every request in the changed collection. Postman assigned/normalized IDs in
live tests; raw fallback behavior is covered offline.

URLs, auth, scripts, headers/query/variable values, bodies and examples are omitted.
Known exact sensitive values and explicit `redact_values` are removed from retained
prose, alongside common credential patterns. Metadata labels are preserved. Arbitrary
secrets embedded in prose are not universally detectable; ingest reviewed documentation.
Raw trees are not retained in state. Sanitized docs and cursors remain in local state,
staging and Cognee storage; protect those directories. Rendering policy changes force
refetches so unchanged cached documents receive current redaction.

Retries are bounded for transport, 429 and 5xx; numeric retry-after waits above 60s
require a later sync. Monthly exhaustion stops further reads. A stable sync costs
`/me`, two workspace checks, three inventory passes and one tree fetch per changed
collection (convergence may need a fourth pass). A small unchanged workspace normally
costs six calls. Budget against actual account headers; do not assume free-plan limits
or schedule hourly by default.

## Validation

```sh
RUNTIME__DLTHUB_TELEMETRY=false python -m pytest tests -q
ruff check .
ruff format --check .
```

60 offline tests pass; one local-model integration test is opt-in. Offline tests cover
provider faults, cursor/cache behavior, redaction, namespace identity, real dlt/SQLite
replacement, interrupted load recovery and actual Cognee staging/read-back.

```sh
# Configure local models first. Run in its own process.
POSTMAN_MODEL_TEST=1 python -m pytest tests/test_cognee_scope_lifecycle.py -q
```

That integration test populates two independent source scopes in one dataset, forgets
one, verifies the other in Data/graph/vectors/search, then verifies complete cleanup.
A clean Python 3.12 installation from PyPI with Cognee 1.6.3 passed the offline suite,
installed-wheel authenticated lifecycle and this scope test. Prior exact-release tests
also injected ingestion and cleanup failures, then proved unchanged retry recovery.
Small synthetic fixtures establish these behaviors; no full Python-version/backend
matrix, every concurrent edit pattern or mid-transaction crash is claimed.

Official references:
- https://learning.postman.com/api-docs/api-reference/collections/get-collections
- https://learning.postman.com/docs/reference/postman-api/authentication/
- https://learning.postman.com/docs/reference/postman-api/postman-api-rate-limits/
