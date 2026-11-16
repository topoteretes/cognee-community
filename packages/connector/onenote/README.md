# OneNote connector for Cognee

Turn selected Microsoft OneNote notebooks into searchable Cognee documents. This
package reads notebook and section metadata, nested section groups, and page HTML
through Microsoft Graph. Page text includes notebook/section breadcrumbs, image
alternative text and resource references. Images and attachments are not downloaded.

## Install

From this repository, with Python 3.10–3.13 and `uv`:

```bash
cd packages/connector/onenote
uv sync --locked --group dev --extra auth
```

The package requires Cognee >=1.6.3,<2 and DLT >=1.16,<2, subject to Cognee's
compatible DLT range. Cognee 1.6.3 imports a temporal helper absent in DLT 1.9;
1.16 is the minimum verified version. The lockfile records the tested resolution. MSAL is an optional
`auth` extra needed by the runnable example; token acquisition stays outside the
connector. Cognee's document-source ingestion path requires `DOCUMENT_SYNC_VERSION >= 1`;
version 1 introduced table-scoped cleanup and confirmed-empty-table reconciliation.
The guard rejects older ingestion paths that cannot safely reconcile removals. No
core changes or database adapter registration are needed.

## Microsoft setup

1. Register an application in the [Microsoft Entra admin center](https://entra.microsoft.com/).
   Choose account types that match the accounts you intend to use. For both personal
   and work/school notebooks, allow organizational accounts and personal Microsoft accounts.
2. In **Authentication**, enable **Allow public client flows** for device-code login.
3. In **API permissions**, add Microsoft Graph **delegated** `Notes.Read`.
   Your organization may require administrator consent.
4. Copy the application/client ID. A client secret is not used by this example.

OneNote requires delegated authentication; its API does not support app-only
authentication. See the [Microsoft OneNote API overview](https://learn.microsoft.com/en-us/graph/api/resources/onenote-api-overview?view=graph-rest-1.0).
This connector supports the public-cloud `graph.microsoft.com` endpoint.

```bash
export MICROSOFT_CLIENT_ID="your-application-id"
export MICROSOFT_TENANT_ID="common"  # or your organization's tenant ID
export LLM_API_KEY="your-provider-key"
uv run --frozen python examples/onenote_example.py
```

The first run prints Microsoft's device-login instructions and lists notebook IDs.
Choose the notebooks to ingest explicitly:

```bash
export ONENOTE_NOTEBOOK_IDS="first-notebook-id,second-notebook-id"
export ONENOTE_DATASET="onenote-notes"
export ONENOTE_QUESTION="What decisions did I record about the launch?"
uv run --frozen python examples/onenote_example.py
```

Re-run after editing a page to update the same dataset. Use Cognee's usual provider
settings if you use an LLM or embedding provider other than its defaults. Notebook
listing does not require an LLM key.

The example keeps tokens in memory unless you set `ONENOTE_TOKEN_CACHE` to a file
path. That optional cache contains credentials and is written with owner-only
permissions. Keep it outside the repository. MSAL attempts silent acquisition and
refresh for the selected account before prompting for another device login. When a
cache contains multiple accounts, set `MICROSOFT_ACCOUNT_ID` to the intended MSAL
`home_account_id`; do not switch accounts while reusing an account scope.

## Python API

```python
from cognee_community_connector_onenote import list_notebooks, onenote_source

notebooks = list_notebooks(token_provider)
resource = onenote_source(
    token_provider,
    notebook_id="selected-notebook-id",
    account_id="stable-account-and-tenant-identity",
)
```

`token_provider` can be a token string or a zero-argument callable returning a token.
A callable should return a valid token for the same account on every request.
Authentication acquisition stays outside the connector. A rejected/expired token
makes synchronization incomplete; inspect failure statistics and reauthenticate.
Notebook listing raises an error. Repeatedly returning a rejected token is not a refresh strategy. The example uses MSAL for acquisition and refresh.

Each call returns one DLT resource for one explicit `notebook_id`. Loop over notebook
IDs to sync several notebooks, as the example does. `account_id` must be a stable,
nonsecret account-and-tenant identity, not an access token or display name. The
example derives it from the authenticated MSAL account and actual tenant claim.
Default resource/table names derive from account and notebook IDs; Cognee scopes
the pipeline by resource name and destination dataset. Preserve these identities
across syncs. An optional `resource_name` names the connection explicitly. It must
be a nonempty, non-whitespace string and is normalized using DLT's table naming
convention; the canonical name is used for the resource, table and pipeline scope.
Names such as `Work Notes` and `work_notes` can normalize to the same connection;
use distinct canonical names for separate connections. Reusing persisted state for
another account or notebook is rejected.

Use foreground merge ingestion:

```python
result = await cognee.add(
    resource,
    dataset_name="onenote-notes",
    run_in_background=False,
    write_disposition="merge",
)
```

Both APIs accept an optional `http_client` for testing. The resource also accepts
`service` as an alias for that client (supply only one), and a zero-argument
`check_active` callback invoked before each HTTP attempt and before publishing
rows. Callback failures raise a dedicated callback error with the original failure
as its cause. `max_requests=300` bounds all HTTP attempts per extraction, including
retries. No page-count, cache-size or nesting-depth cutoff truncates a notebook.
Increase the budget if complete metadata enumeration alone consumes it; repeating
an insufficient budget cannot guarantee completion.

After ingestion inspect `resource.cognee_sync_stats`. It starts empty until
extraction runs. Extraction initializes integer counters: `scanned`, `skipped`,
`failed`, `deleted`, `failed_rate_limit`, `failed_budget`, `requests`, and `complete`.
You must inspect these statistics: a normal extraction return does not establish
successful synchronization. Require `failed == 0` and `complete == 1` before graph
processing. `skipped` counts unchanged page bodies reused from the cache, not bad
pages omitted from ingestion. An empty dictionary means DLT resumed a pending load
without extracting this resource; create a fresh resource and run ingestion again.
The example allows one such recovery retry.

## Sync and deletion behavior

Every sync enumerates complete metadata for its selected notebook. This is
incremental **content fetching**, not a Graph delta feed: HTML is fetched for new
pages, changed page timestamps, missing cache entries or a changed renderer
version. Unchanged page bodies are reused, while current metadata rebuilds titles,
breadcrumbs and links. A new page is fetched even if its timestamp is older than
previously seen pages. Timestamp-only changes do not alter an otherwise identical
emitted document.

The connector validates the complete notebook snapshot before emitting document
rows or explicit delete records. A failed traversal, fetch, permission check,
malformed page or exhausted retry blocks publication for the whole notebook; bad
pages are not silently skipped. Successfully parsed page bodies and confirmed
removals are checkpointed in resource state. Expected provider failures, including
rate limits, malformed metadata and budget exhaustion, return normally so DLT can
persist those checkpoints. Such a run reports `failed > 0` and `complete == 0` and
emits no document rows or delete records; rerun after addressing the failure to
reuse saved progress. Callback failures and unexpected programming errors raise
instead of being treated as successful checkpoints.

HTTP requests use a 30-second timeout and at most five total attempts for transient
failures. Retry waits have a cumulative 60-second budget per request to avoid
holding Cognee's staging lock through prolonged waits. When Microsoft's
`Retry-After` exceeds the remaining wait budget, the request stops rather than
retrying earlier than requested. Pagination requests `createdDateTime asc` ordering,
following [Microsoft's page-query guidance](https://learn.microsoft.com/en-us/graph/onenote-best-practices),
and follows next links or top/skip batches. ID-based duplicate and cycle checks
reject conflicting responses. Creation-time ordering avoids reordering on edits,
but Microsoft listings are not a frozen transaction; concurrent changes may still
require another sync.

Missing cached pages are checked directly before removal. Microsoft OneNote's
explicit deleted-resource code `20113` can confirm deletion. A nonexistent-page
code `20102` is accepted after a complete same-account traversal with its
notebook still accessible, or when its parent was positively confirmed deleted
with `20113`. Generic 404s, invalid IDs and access failures are not
proof of deletion. Confirmed parent deletion reconciles descendants individually,
including pages that moved. An inaccessible or ambiguously missing parent cannot
guarantee next-sync removal of its old pages. See [OneNote error codes](https://learn.microsoft.com/en-us/graph/onenote-error-codes).

A page positively confirmed to have moved outside the notebook being synced is
removed from that notebook's memory; the excluded body is not fetched. Sync its
new notebook separately to ingest it there. An uncertain move or access failure
preserves the existing copy. Omitting a notebook from future calls preserves its
memory: deselection is not deletion.

Confirmed removals survive interrupted runs, so a large set of missing pages can
be checked over several extractions without repeating completed confirmations.
Each run traverses fresh metadata. Observing a page reappear immediately invalidates
its saved confirmation, even if that traversal later fails. Remaining missing
pages still require direct confirmation; ambiguous responses preserve memory and
block publication. Rows and deletions are
published only after reconciliation finishes, then its confirmation checkpoint
is cleared.

Loading uses merge with page IDs as keys and explicit hard-delete records. On a
confirmed final-page deletion, the staging table physically reaches zero rows;
no fake empty document is inserted. Cognee reconciles that successfully read table
and removes obsolete documents and their owned graph/vector artifacts. An override
to replace disposition is rejected before fetching or emitting changes, because
an interrupted extraction must never empty the table.

## Failure recovery and operation limits

Run one active sync per account/dataset scope. This package does not provide a
cross-process synchronization lock. It materializes selected snapshots and stores
parsed bodies in DLT resource state, so large notebooks consume both memory and
disk. Each parsed body is stored once, alongside previously published page IDs;
rendered documents are rebuilt from current metadata. Evaluate that cost before
increasing the request budget. Keep Cognee's
storage and DLT working directory persistent; deleting state forces content to be
fetched again. The cache contains notebook content and should receive the same
access controls as your notes.

The 300-request default is a conservative per-extraction choice, not enforcement
of Microsoft's time-based quotas. Microsoft documents delegated limits of
120 requests per minute and 400 per hour per app/user; repeated runs and other
notebooks share that allowance. See [OneNote service limits](https://learn.microsoft.com/en-us/graph/throttling-limits#onenote-service-limits).
Even with no changed bodies, metadata costs at least one notebook-list request,
two collection requests per notebook or section-group container, and one page-list
request per section. Pagination, content downloads, retries and removal checks add
requests. Schedule runs accordingly; increasing `max_requests` does not increase
the provider allowance.

The example explicitly checks foreground `cognee.add()` completion and per-item
failures, then checks `cognee.cognify()` before calling `cognee.recall()`. It checks connector completion statistics and handles DLT resuming an earlier
pending load without extracting the current resource: one additional ingestion
requires a fresh snapshot extraction for each notebook.

Snapshot validation, DLT staging, document ingestion, orphan cleanup and graph/
vector processing are separate stages, not one atomic transaction. After a later
processing failure, rerun the same selection: the complete cached snapshot is
replayed so unchanged upstream timestamps do not prevent recovery. Cognee's
orphan cleanup is best effort; inspect its logs and rerun after a cleanup failure.
Use foreground ingestion for reconciliation; background ingestion does not run
the same orphan-cleanup step in Cognee 1.6.3.

This is the initial, unpublished `0.1.0` package. Earlier development revisions
used different provider tags, source-state layouts and table identities; those
connections are not automatically migrated or cleaned up. Use a separate dataset
or deliberately clean up the old source's documents and state before switching.
Within the current resource cache, the earlier full-row mapping is converted to
published IDs without changing connection identity or refetching cached bodies.

## Verification

```bash
uv run --frozen python -m pytest tests -ra
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv build
```

Offline tests use mocked Graph responses. Local integration tests exercise DLT
staging and Cognee persistence with external model calls mocked. Neither proves
that Microsoft accepted a real application's credentials or live deletion
responses. Live verification requires your own app, consenting account and
disposable selected notebooks. Record the results of initial ingestion, unchanged
rerun, edit, rename, move and final-page deletion separately from offline results.

This package implements [topoteretes/cognee#4727](https://github.com/topoteretes/cognee/issues/4727)
in the community repository.
