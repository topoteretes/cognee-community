# OneNote connector for Cognee

Turn selected Microsoft OneNote notebooks into searchable Cognee documents. This
package reads notebook and section metadata, nested section groups, and page HTML
through Microsoft Graph. Page text includes notebook/section breadcrumbs, image
alternative text and resource references. Images and attachments are not downloaded.

## Install

From this repository, with Python 3.11–3.13 and `uv`:

```bash
cd packages/connector/onenote
uv sync --locked --group dev
```

The package targets Cognee 1.6.3 and DLT 1.30.0. It uses Cognee's document-source
ingestion path; no Cognee core changes or database adapter registration are needed.

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
source = onenote_source(
    token_provider,
    notebook_ids=["selected-notebook-id"],
    account_id="stable-account-and-tenant-identity",
)
```

`token_provider` can be a token string or a zero-argument callable returning a token.
A callable should return a valid token for the same account on every request.
Authentication acquisition stays outside the connector. A rejected/expired token
raises a reauthentication error; repeatedly returning the same rejected token is
not a refresh strategy. The example uses MSAL for acquisition and refresh.

`notebook_ids` must be a nonempty explicit selection. `account_id` must be a stable,
nonsecret account-and-tenant identity, not an access token or display name. The
example derives it from the authenticated MSAL account and actual tenant claim.
Account scope determines the DLT source and notebook-specific table names; Cognee
additionally scopes the pipeline by destination dataset. Preserve these identities
and the dataset name across syncs.

Both APIs accept an optional `http_client` for testing. The source also accepts
`max_pages=1000` and `max_cache_bytes=16777216` to bound its cached state. Limits
include retained caches for temporarily deselected notebooks. Exceeding a limit
fails the sync without publishing a truncated snapshot.

## Sync and deletion behavior

Every sync enumerates complete metadata for the selected notebooks. This is
incremental **content fetching**, not a Graph delta feed: HTML is fetched for new
pages, changed page timestamps, missing cache entries or a changed renderer
version. Unchanged page bodies are reused, while current metadata rebuilds titles,
breadcrumbs and links. A new page is fetched even if its timestamp is older than
previously seen pages. Timestamp-only changes do not alter an otherwise identical
emitted document.

The connector validates the complete selected snapshot before emitting rows. A
failed metadata traversal, page fetch, permission check or exhausted retry does
not authorize deletion. HTTP requests use a 30-second timeout, at most five total
attempts for transient failures and Microsoft `Retry-After` guidance for throttling.
Pagination follows next links or explicit top/skip batches, with cycle and
conflicting-response checks. Microsoft listings are not a frozen transaction;
changes made while enumerating may require another sync.

Missing cached pages are checked directly before removal. Microsoft OneNote's
explicit deleted-resource code `20113` can confirm deletion. A nonexistent-page
code `20102` is accepted only after a complete same-account traversal with its
notebook still accessible. Generic 404s, invalid IDs and access failures are not
proof of deletion. Confirmed parent deletion reconciles descendants individually,
including pages that moved. An inaccessible or ambiguously missing parent cannot
guarantee next-sync removal of its old pages. See [OneNote error codes](https://learn.microsoft.com/en-us/graph/onenote-error-codes).

A page found in another selected location is reconciled with its current
breadcrumbs. If it moved outside the selection, its previously ingested copy is
retained with a warning; the newly excluded body is not fetched. Removing a
notebook from `notebook_ids` preserves its existing memory and cached state.
Deselection is not deletion.

Confirmed deletion of a notebook's final page emits a schema-only empty-table
marker, so DLT physically replaces that table with zero rows. No fake empty
document is inserted. Cognee reconciles the successfully loaded notebook table
and removes obsolete documents and their owned graph/vector artifacts.

## Failure recovery and operation limits

Run one active sync per account/dataset scope. This package does not provide a
cross-process synchronization lock. It materializes selected snapshots and stores
parsed bodies in DLT source state, so large notebooks consume both memory and
disk. Increase the explicit limits only after evaluating that cost. Keep Cognee's
storage and DLT working directory persistent; deleting state forces content to be
fetched again. The cache contains notebook content and should receive the same
access controls as your notes.

The example explicitly checks foreground `cognee.add()` completion and per-item
failures, then checks `cognee.cognify()` before calling `cognee.recall()`. It also
handles DLT resuming an earlier pending load without extracting the current
source: one additional ingestion requires a fresh snapshot extraction.

Snapshot validation, DLT staging, document ingestion, orphan cleanup and graph/
vector processing are separate stages, not one atomic transaction. After a later
processing failure, rerun the same selection: the complete cached snapshot is
replayed so unchanged upstream timestamps do not prevent recovery. Cognee's
orphan cleanup is best effort; inspect its logs and rerun after a cleanup failure.
Use foreground ingestion for reconciliation; background ingestion does not run
the same orphan-cleanup step in Cognee 1.6.3.

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
