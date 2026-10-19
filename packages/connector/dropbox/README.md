# cognee-community-connector-dropbox

A Dropbox data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync Dropbox folders into memory, incrementally, and forget files when they are deleted.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each file becomes a
**normal document** in cognee (it flows through chunking and LLM entity extraction),
via cognee's document-mode marker.

## What it syncs

| Dropbox file | How it is read |
|---|---|
| `.txt`, `.md`, `.markdown`, `.csv` | downloaded as text |
| `.pdf` | text extracted with `pypdf` |
| Paper docs and other non-downloadable files | exported through `files/export` (markdown when offered) |
| anything else, files over `max_file_size_mb`, empty or corrupt files | skipped with a warning and counted in the sync stats |

Folders are synced recursively. Each document starts with the file's Dropbox path and
folder, so the graph can relate files that live together.

## Requirements

- cognee **1.6.3 or newer** (below 2). The connector relies on table-scoped document
  cleanup (`DOCUMENT_SYNC_VERSION`), `PIPELINE_SCOPE_ATTR` and `guarded_rows`.
- Python 3.10 to 3.13.

## Install

```bash
uv pip install cognee-community-connector-dropbox
# or, from this monorepo:
cd packages/connector/dropbox && uv sync
```

## Setup

1. Create an app at <https://www.dropbox.com/developers/apps>: **Scoped access**, then
   **App folder** (the app sees only `Apps/<your app>/`) or **Full Dropbox**.
2. In the app's **Permissions** tab, enable `files.metadata.read` and
   `files.content.read`, and click **Submit**. Nothing else is needed.
3. Copy the **App key** from the **Settings** tab, then get a refresh token once:

   ```bash
   DROPBOX_APP_KEY=<app key> uv run python examples/get_refresh_token.py
   ```

   It uses the OAuth 2 PKCE flow with `token_access_type=offline`, so no app secret is
   needed and the refresh token does not expire.
4. For `examples/example.py`, set the environment, plus your `LLM_API_KEY` like any
   other cognee run:

   ```bash
   export DROPBOX_APP_KEY=<app key>
   export DROPBOX_REFRESH_TOKEN=<refresh token>
   export DROPBOX_FOLDER_PATHS=/Notes,/Work   # optional, default is the whole Dropbox
   ```

## Usage

```python
import os

import cognee
from cognee_community_connector_dropbox import dropbox_source

await cognee.remember(
    dropbox_source(
        folder_paths=["/Notes"],
        refresh_token=os.environ["DROPBOX_REFRESH_TOKEN"],
        app_key=os.environ["DROPBOX_APP_KEY"],
    ),
    dataset_name="my_dropbox",
    primary_key="id",
    write_disposition="merge",  # required: incremental upserts and deletes
    max_rows_per_table=0,  # no row cap; folders often exceed the default 50
)

answer = await cognee.recall("What are my notes about?", datasets=["my_dropbox"])
```

Run the same `remember` call again whenever you want to sync. See
`examples/example.py` for the full flow; it reads the `DROPBOX_*` environment variables.
`dropbox_source()` itself takes everything as arguments.

| Argument | Default |
|---|---|
| `folder_paths` | whole Dropbox (the app folder for App folder apps) |
| `refresh_token` + `app_key` | – |
| `app_secret` | unset, not needed for PKCE tokens |
| `access_token` | short-lived, for quick tries only |
| `max_file_size_mb` | 25 |
| `resource_name` | `dropbox_files`. Lowercase letters, digits and underscores, up to 64. **Two Dropbox accounts in one dataset need different names**, or they would share one table and one sync state |
| `check_active` | optional host callback, asked before every Dropbox request |
| `service` | a pre-built Dropbox client, mainly for tests (`client` still works) |

After a run, `source.cognee_sync_stats` holds counts only, never file names or content:
`scanned`, `skipped` (plus `skipped_<reason>`), `failed`, `deleted`, and `failed_auth` /
`failed_rate_limit` for files left for the next run because of a 401 or a rate limit.

## How sync and forget-on-delete work

- **First run:** `list_folder` (recursive) lists every file, and the cursor is saved in
  dlt state, one per folder.
- **Later runs:** `list_folder/continue` returns only what changed. The cursor is saved
  even when nothing changed, because unused cursors can expire.
- **Deletes carry no id.** Dropbox reports a deletion as a path, so the connector keeps a
  `path_lower -> id` index in dlt state and turns deleted paths back into file ids.
  A deleted folder forgets every file under its path.
- **Moves and renames** arrive as a deleted old path plus the same id at a new path.
  Deletions are held until every folder is read, so a moved file is updated in place,
  not forgotten and re-learned.
- Deleted files are emitted as `{"id": ..., "_deleted": True}` with a `hard_delete`
  column. dlt removes them on `merge`, and cognee's orphan cleanup removes them from the
  graph and vector stores.
- **Errors never delete.** Only a confirmed `not_found` removes data. A reset cursor
  (`409 reset`) triggers a fresh listing reconciled against the index.
- **Failed downloads are retried by id.** The cursor always moves forward. A file that
  failed to download (a 5xx or a network error) is kept in a `retry` list in dlt state and
  fetched again by id next run, so one bad file never makes later runs replay everything.
  A file that keeps failing costs one extra request per run and keeps `failed` at 1 or
  more. Files that can never be downloaded, like restricted content, are skipped instead.
- **Rate limits.** A 429 is waited out up to 60 seconds, at most 3 times, with
  `check_active` asked again before each retry. A longer wait is never slept through,
  since cognee holds its dlt staging lock during a sync: the run stops, keeps what it
  stored, and the remaining files are fetched next run.
- **An expired or revoked token mid-run** stops the run the same way, since every later
  request would fail too. If nothing was stored yet, the error is raised: that is just a
  bad token.
- **One failing folder fails the run.** Folders are not independent here: a file moved
  from one synced folder into another shows up as a delete in the first. If the second
  folder could not be read, the move would look like a delete, so the run stops before
  anything is emitted and dlt keeps the previous state.
- **Host checkpoint:** `check_active` runs before every Dropbox request, outside any error
  handling, so a revoked connection stops the sync between two calls and is never counted
  as a failed file.

## Privacy

- The connector only **reads**. It asks for `files.metadata.read` and
  `files.content.read`, and never writes, moves or shares anything in Dropbox.
- With an **App folder** app, it can only see `Apps/<your app>/`.
- File content is sent to your configured LLM and embedding providers during cognify,
  like any other document you add to cognee. Choose local models if that matters.
- dlt state stores folder cursors and the `path_lower -> id` index, so file **paths** are
  kept locally; contents are not.
- Logs and sync stats carry counts and skip reasons only: never file contents, names,
  paths or Dropbox ids.
- Everyone who can read the cognee dataset can read the synced file contents.
- Keep the refresh token out of your repo. Revoke it any time in Dropbox under
  **Settings → Connected apps**.

## Limitations

- Team spaces (the `Dropbox-API-Path-Root` header) are not supported yet; the connector
  reads the user's own namespace.
- Files are matched to a parser by extension.
- Rows have no web URL: for App folder apps the paths are relative to the app folder, so
  a reliable link cannot be built.
- Removing a folder from `folder_paths` forgets the files that only it covered.

## Testing

```bash
uv run pytest tests/
```

No Dropbox account or API key is needed. `tests/fake_dropbox.py` is an in-memory Dropbox
that returns the official SDK's own metadata and error types and replays a change log
through cursors.

- `test_dropbox_sync.py` covers first sync, pagination, edits, moves (within, between
  and out of synced folders), file and folder deletes, a path reused by a new file,
  cursor reset, a vanished folder, listing errors that must not delete, failed downloads
  retried by id without growing later runs, short and long rate limits, an expired token
  mid-run, a repeated cursor, restricted files, PDF and Paper export, skip reasons, folder
  configuration, `check_active` before every request (listing pages, incremental rounds and
  downloads) and never swallowed by the download error handling, and no file names, paths
  or ids in the logs.
- `test_dropbox_source.py` covers `dropbox_source()` itself: credentials as arguments only,
  refusing an empty folder list and an unsafe `resource_name`, folder normalization and
  the document-mode and pipeline-scope markers.
- `test_dropbox_forget.py` runs the real cognee pipeline (LLM and embeddings mocked,
  working with both cognee model variants, including the one used with Gemini) and checks
  that a deleted file and a deleted folder leave the graph and the dataset while a moved
  file stays, and that two Dropbox sources in one dataset never touch each other's files.
  It fails if the tombstone or the `hard_delete` column is turned off.
