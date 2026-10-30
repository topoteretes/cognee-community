# cognee-community-connector-evernote

An Evernote data-source connector for [cognee](https://github.com/topoteretes/cognee):
ingest Evernote notes (with notebook and tag metadata) into cognee as documents, with
incremental synchronization via sync chunks, a persisted USN cursor, and forget-on-delete.

It exposes a `dlt` resource you pass to `cognee.add(...)` or `cognee.remember(...)`. Notes are
extracted with their ENML content converted to clean plain text, scoped by notebook or tag,
and ingested as **normal documents** flowing through cognee's document-mode pipeline.

## Install

```bash
uv pip install cognee-community-connector-evernote
# or, from this monorepo:
cd packages/connector/evernote && uv sync --all-extras
```

## Prerequisites and Access

To connect to Evernote, you need either an existing developer/access token or an Evernote
developer API key (consumer key and consumer secret).

> [!NOTE]
> Evernote's developer program is reportedly gated; check Evernote's developer pages for
> the current status. All automated tests in this package run offline using fake API clients
> and mock responses; this connector has **not** been verified against a live Evernote account.

## Authentication

You can authenticate in two ways:

### 1. Existing Access Token or Developer Token

Provide your token directly to `evernote_source(token=...)` or set the `EVERNOTE_TOKEN`
environment variable:

```bash
export EVERNOTE_TOKEN="your-token"
```

### 2. 3-Legged OAuth 1.0a

Use the built-in `EvernoteOAuth` helper to obtain an access token:

```python
import os
from cognee_community_connector_evernote import EvernoteOAuth, evernote_source

# Reads EVERNOTE_CONSUMER_KEY and EVERNOTE_CONSUMER_SECRET from env
oauth = EvernoteOAuth(
    consumer_key=os.environ["EVERNOTE_CONSUMER_KEY"],
    consumer_secret=os.environ["EVERNOTE_CONSUMER_SECRET"],
    sandbox=False,  # Set True for sandbox.evernote.com
)

# Step 1: Request token
oauth_token, oauth_token_secret = oauth.get_request_token(
    callback_url="https://example.com/callback"
)

# Step 2: User authorization URL
auth_url = oauth.authorize_url(oauth_token)
print(f"Authorize at: {auth_url}")

# Step 3: Exchange verifier for access token
oauth_verifier = input("Enter verifier from callback: ")
access_token_info = oauth.get_access_token(oauth_token, oauth_token_secret, oauth_verifier)
print(f"Access token acquired (user ID: {access_token_info.user_id})")
token = access_token_info.token

# Pass the obtained token to the source
source = evernote_source(token=token)
```

For a complete runnable script, see `examples/example.py`. You can also try it without credentials in an isolated in-memory demo by running:

```bash
python examples/example.py --fake
```

## Usage

```python
import cognee
from cognee_community_connector_evernote import evernote_source

# Create the Evernote dlt resource
source = evernote_source(
    token=None,  # Falls back to EVERNOTE_TOKEN env var
    sandbox=False,  # True for sandbox.evernote.com
    notebook_names=["Engineering", "Projects"],  # Optional notebook filter
    tag_names=["cognee", "docs"],  # Optional tag filter
    resource_name="evernote_notes",  # DLT resource and table name
    full_resync=False,  # Set True to force a full resync
    client=None,  # Optional custom EvernoteApi client for testing
)

# Ingest into cognee using merge write disposition (matching sibling connectors)
await cognee.remember(
    source,
    dataset_name="evernote",
    write_disposition="merge",
    max_rows_per_table=0,  # 0 = no row cap (dlt pipelines commonly exceed the default 50)
)

# Ask questions or search the ingested notes
answer = await cognee.search(
    query_text="Summarize the project roadmap from my Evernote notes.",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["evernote"],
)
```

### Parameters for `evernote_source(...)`

- `token` (`str | None`): Evernote developer or OAuth access token (falls back to `EVERNOTE_TOKEN`).
- `sandbox` (`bool`): When `True`, connects to `sandbox.evernote.com` instead of `www.evernote.com`.
- `notebook_names` (`list[str] | None`): List of notebook names to include (case-sensitive).
- `tag_names` (`list[str] | None`): List of tag names to include (case-sensitive).
- `resource_name` (`str`): DLT resource and table name (defaults to `"evernote_notes"`). Bound to account/sandbox to prevent cross-account pollution.
- `full_resync` (`bool`): When `True`, forces a full pass from USN 0 and deletion reconciliation.
- `client` (`EvernoteApi | None`): Injected API client for offline testing.

## How Incremental Sync Works

Sync state is tracked using Evernote's Update Sequence Number (USN) cursor stored in dlt's persisted resource state (`dlt.current.resource_state()`):
- **First run**: Pulls metadata and notes starting from USN 0 and checkpoints `last_usn`, `last_sync_time`, notebook/tag ID maps, and `note_ids`.
- **Incremental runs**: Only requests sync chunks with `afterUSN = state["last_usn"]`. If server `updateCount == state["last_usn"]`, nothing changed and no chunk calls are made.
- **Transactional state persistence**: The cursor is updated in dlt resource state only after a completely successful run. If an extraction fails or is interrupted, state remains untouched and the next run safely retries from the old cursor.
- **Resync triggers**: A full resync (re-evaluating from USN 0) occurs automatically if `full_resync=True`, if the server's `fullSyncBefore` exceeds `state["last_sync_time"]`, or if the server's `updateCount` is lower than `state["last_usn"]`.
- **Account binding**: The resource state records `account` (e.g. `production:12345` or `sandbox:12345`). Attempting to reuse an existing resource state with a different Evernote user ID or environment raises `EvernoteError` before making any chunk calls.

## How Deletions Work

- Notes marked inactive (`active=False`, trashed) or listed in `expungedNotes` are emitted as tombstone records (`{"id": guid, "title": "", "content": "", "_deleted": True}`).
- Notes moved out of scope (e.g. moved to an unselected notebook or untagged) also emit tombstones so they are removed from the dataset.
- A note whose body fetch raises `NoteNotFoundError` or whose converted body and title are both empty is also tombstoned.
- During a full resync (`full_resync=True`, or triggered by server `fullSyncBefore` / lower `updateCount`), notes previously ingested by this resource (tracked in `state["note_ids"]`) that no longer appear in the server listing are reconciled and emitted as tombstones. This reconciliation sweep runs only after the complete server listing has succeeded.
- Under `write_disposition="merge"`, dlt removes the tombstoned records from staging.
- Cognee's cleanup removes the corresponding `Data` records.
- **Test verification scope**: Tests in this package verify that the corresponding `Data` records are removed from cognee's dataset. (They do *not* assert or test graph or vector database store cleanups).

## Scope Selection

Filter ingested notes with `notebook_names` and `tag_names`:
- **Both provided**: Notes must match **both** filters (must reside in one of the selected notebooks AND carry at least one of the selected tags).
- **Exact matching**: Names are compared strictly and case-sensitively against server notebook and tag names.
- **Unknown names**: Passing a notebook or tag name that does not exist in the Evernote account raises `EvernoteError`.

## Limitations

- **No live-API validation**: Tested exclusively offline against fake clients and simulated sync chunks; has not been run against a live Evernote account.
- **Pre-release dependency**: Relies on `evernote-plus==1.28.1.dev2` for Python 3 EDAM Thrift bindings.
- **Notebook/tag renames**: Renaming a notebook or tag on the server updates the internal ID mapping on the next run, but does not re-emit unchanged notes (notes retain their previously rendered metadata until edited or until a `full_resync=True` run is performed).
- **Expunged notebook behavior**: Evernote API behavior for notes inside an expunged notebook is UNKNOWN.
- **USN cursor advances on filtered objects**: If changes on the server only affect unrequested object types (e.g. saved searches or resources), the cursor in state may remain below the server's `updateCount` (this is harmless and may trigger one extra empty sync chunk check).
- **Attachments**: Images, audio, PDFs, and binary attachments in ENML are replaced with text placeholders (`[Attachment: type=...]`) and their raw bytes are not ingested.

## Running the Tests

Run the full offline verification suite with `uv`:

```bash
uv run pytest tests/ -ra
uv run ruff check .
uv run ruff format --check .
uv build
```
