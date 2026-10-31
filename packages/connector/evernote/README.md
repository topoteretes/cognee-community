# cognee-community-connector-evernote

An Evernote data-source connector for [cognee](https://github.com/topoteretes/cognee):
ingest Evernote notes (with notebook and tag metadata) into cognee as documents, with
incremental synchronization via sync chunks, a persisted USN cursor, and forget-on-delete.

It exposes a `dlt` resource you pass to `cognee.add(...)` or `cognee.remember(...)`. Notes are
extracted with their ENML content converted to clean plain text (prefixed with optional
`Notebook: <name>` and `Tags: <tags>` header lines for semantic search), scoped by notebook
or tag, and ingested as **normal documents** flowing through cognee's document-mode pipeline.

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

Provide your token directly to `evernote_source(token=...)`. Note that `token` is a required
argument; pass your token string directly.

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
    token="your-token",  # Required Evernote access token
    sandbox=False,  # True for sandbox.evernote.com
    notebook_names=[
        "Engineering",
        "Projects",
    ],  # Optional notebook filter (empty list means no filter)
    tag_names=["cognee", "docs"],  # Optional tag filter (empty list means no filter)
    resource_name="evernote_notes",  # DLT resource and table name
    full_resync=False,  # Set True to force a full resync
    service=None,  # Optional custom EvernoteApi service for testing
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

- `token` (`str`): Evernote developer or OAuth access token (required; blank token raises `EvernoteAuthError`).
- `sandbox` (`bool`): When `True`, connects to `sandbox.evernote.com` instead of `www.evernote.com`.
- `notebook_names` (`list[str] | None`): List of notebook names to include (case-sensitive; empty list or `None` means no filter).
- `tag_names` (`list[str] | None`): List of tag names to include (case-sensitive; empty list or `None` means no filter).
- `resource_name` (`str`): DLT resource and table name (defaults to `"evernote_notes"`). Bound to account/sandbox to prevent cross-account pollution.
- `full_resync` (`bool`): When `True`, forces a full pass from USN 0 and deletion reconciliation.
- `service` (`EvernoteApi | None`): Injected API service for offline testing.
- `max_notes_per_run` (`int | None`): Maximum note bodies to fetch per run (defaults to `100`, a conservative batch limit not derived from Evernote's published limits; `None` for unlimited).
- `check_active` (`Callable[[], bool] | None`): Optional callback invoked before each API call; returning `False` pauses the run cleanly, committing rows fetched so far and saving partial progress to resume on the next run.

## How Incremental Sync Works

Sync state is tracked using Evernote's Update Sequence Number (USN) cursor stored in dlt's persisted resource state (`dlt.current.resource_state()`):
- **State version**: The connector uses schema version `v = 1`. If an existing state lacks `v = 1` or has legacy list-formatted `note_ids`, it is treated as a first run and safely re-synced once, overwriting the state with version 1.
- **First run**: Pulls metadata and notes starting from USN 0 and checkpoints `v`, `account`, `last_usn`, `last_sync_time`, notebook/tag ID maps, `note_ids` (`{note_guid: notebook_guid}`), and `scope`.
- **Incremental runs**: Only requests sync chunks with `afterUSN = state["last_usn"]`. If server `updateCount == state["last_usn"]`, nothing changed on the server; no chunk calls are made and state keys (including `last_sync_time`) are left untouched, preventing spurious dlt pipeline state revisions.
- **Batch limits and multi-run sync**: By default, each sync run limits note body fetches to `max_notes_per_run=100`. For large accounts, the initial sync progresses across multiple runs. An unfinished run records progress via `state["partial"] = {"resume_usn": N, "full": bool, "scope": ...}` and commits the notes fetched so far without advancing `last_usn` or `last_sync_time`. The subsequent run picks up where the previous run left off, skipping already-fetched notes (`updateSequenceNum <= resume_usn`) while refetching any note edited since. Once pass 2 completes, `state["partial"]` is removed and the USN cursor is advanced.
- **Rate limit resilience**: If an Evernote API rate limit is encountered during note body fetching, or the `max_notes_per_run` budget is exhausted, the sync run ends cleanly with a warning (logging notes remaining and retry delay if provided) instead of raising an unhandled exception. Notes loaded up to that point are saved and committed to Cognee, allowing subsequent runs to resume automatically. If rate-limited during Pass 1 metadata loading, the run ends cleanly with a warning without mutating state.
- **Transactional state persistence**: The cursor and state keys are updated in dlt resource state only after a completely successful run and only when their values actually changed. If an extraction fails or is interrupted, state remains untouched and the next run safely retries from the old cursor.
- **Resync triggers**: A full resync (re-evaluating from USN 0) occurs automatically if `full_resync=True`, if the server's `fullSyncBefore` exceeds `state["last_sync_time"]`, if the server's `updateCount` is lower than `state["last_usn"]`, or if `notebook_names` / `tag_names` scope, missing persisted GUID lists, or resolved GUIDs change.
- **Scope changes and swap detection**: Configured names are resolved to GUIDs every run. If names change or if another notebook/tag reuses a configured name with a different GUID (a scope swap), the run restarts pass 1 from USN 0 with fresh maps so previously ingested out-of-scope notes are tombstoned and newly in-scope notes load.
- **Account binding**: The resource state records `account` (e.g. `production:12345` or `sandbox:12345`). Attempting to reuse an existing resource state with a different Evernote user ID or environment raises `EvernoteError` before making any chunk calls.

## How Deletions Work

- Notes marked inactive (`active=False`, trashed) or listed in `expungedNotes` are emitted as tombstone records (`{"id": guid, "title": "", "content": "", "_deleted": True}`).
- **Expunged notebooks**: When a notebook is expunged (`expungedNotebooks`), all notes previously ingested under that notebook GUID (tracked in `state["note_ids"]`) are emitted as tombstones and removed from state, deduplicated against `expungedNotes`. If a note was moved to another notebook in the same run's metadata, the new notebook GUID wins and the note is preserved.
- Notes moved out of scope (e.g. moved to an unselected notebook or untagged) also emit tombstones so they are removed from the dataset.
- A note whose body fetch raises `NoteNotFoundError` or whose converted body and title are both empty is also tombstoned.
- During a full resync (`full_resync=True`, or triggered by server `fullSyncBefore` / lower `updateCount` / scope change), notes previously ingested by this resource (tracked in `state["note_ids"]`) that no longer appear in the server listing are reconciled and emitted as tombstones. This reconciliation sweep runs only after the complete server listing has succeeded.
- Under `write_disposition="merge"`, dlt removes the tombstoned records from staging.
- Cognee's cleanup removes the corresponding `Data` records.
- **Test verification scope**: Tests in this package verify that the corresponding `Data` records are removed from cognee's dataset. (They do *not* assert or test graph or vector database store cleanups).

## Scope Selection

Filter ingested notes with `notebook_names` and `tag_names`:
- **Both provided**: Notes must match **both** filters (must reside in one of the selected notebooks AND carry at least one of the selected tags).
- **Exact matching**: Names are compared strictly and case-sensitively against server notebook and tag names.
- **Unknown or renamed names**: Passing a notebook or tag name that does not exist in the Evernote account raises `EvernoteError` ("Notebook/Tag name(s) not found: [...]. If a notebook/tag was renamed, update notebook_names/tag_names.") without writing any state.
- **Scope changes trigger full resync**: Changing `notebook_names` or `tag_names`, or swapping the underlying notebook/tag GUID for a configured name, automatically triggers a full resync with deletion reconciliation — notes no longer in scope are tombstoned and removed from cognee's memory.

## Limitations

- **No live-API validation**: Tested exclusively offline against fake clients and simulated sync chunks; has not been run against a live Evernote account.
- **Pre-release dependency**: Relies on `evernote-plus==1.28.1.dev2` for Python 3 EDAM Thrift bindings.
- **Notebook/tag renames**: notebook and tags are rendered as Notebook:/Tags: lines at the top of each note; a rename changes that text, but the notes are only re-emitted when edited or with full_resync=True; a rename can also change which notes are in scope (see the scope/guid text).
- **USN cursor advances on filtered objects**: If changes on the server only affect unrequested object types (e.g. saved searches or resources), the cursor in state may remain below the server's `updateCount` (this is harmless and may trigger one extra empty sync chunk check).
- **Attachments**: Images, audio, PDFs, and binary attachments in ENML are replaced with text placeholders (`[attachment: {media_type}]`) and their raw bytes are not ingested.

## Running the Tests

Run the full offline verification suite with `uv`:

```bash
uv run pytest tests/ -ra
uv run ruff check .
uv run ruff format --check .
uv build
```
