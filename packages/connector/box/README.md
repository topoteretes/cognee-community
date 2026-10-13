# Box connector

Ingest selected Box folders, file text and comments as searchable Cognee documents.
An initial folder crawl captures the user-events cursor first; later runs fetch affected
items and emit changed documents or confirmed deletion tombstones. Use the same dataset
and `merge` configuration for every sync.

Box's user-events feed does not guarantee comment-edit notifications. When comments
are selected, each sync also lists comments on every known active file, then emits only
changed comments. Unchanged file content is not downloaded. This adds at least one
API request per file per run, plus comment pagination; selecting only files/folders
avoids that polling cost. Sync duration grows with selected file and comment counts.

## Install

From this directory:

```sh
uv sync --dev
```

Python 3.11-3.13, Cognee 1.6.3, dlt with the SQLAlchemy destination and Box SDK 3.x are
required. Configure Cognee's LLM and embedding providers before ingestion; local
OpenAI-compatible providers work when configured in Cognee.

## Connect

| Authentication | Setup |
|---|---|
| OAuth 2.0 | Create an OAuth app in the [Box Developer Console](https://app.box.com/developers/console). Enable read access to files/folders. Register `http://localhost:8765/callback` as the redirect URI. Set `BOX_CLIENT_ID`, `BOX_CLIENT_SECRET`, `BOX_TOKEN_PATH`, then run `uv run python examples/authorize.py` and approve in the browser. |
| JWT | Create a JWT app, generate its key pair, obtain enterprise administrator approval, and download the private JSON configuration. Set `BOX_JWT_CONFIG_PATH` to that file. Share the selected folders with the app's service account. This uses that account's user-event stream, not enterprise-wide events. |

OAuth access and refresh tokens are saved together and replaced on refresh. Keep the
token file and JWT configuration outside Git with access limited to the executing user.
Run one sync process per token file; concurrent refreshes of the same single-use token
are unsupported. Existing access/refresh tokens can bootstrap `BOX_TOKEN_PATH` using
`BOX_ACCESS_TOKEN` and `BOX_REFRESH_TOKEN`. A Developer Token alone is insufficient.

## Sync and search

Set `BOX_FOLDER_IDS` to comma-separated numeric folder IDs from Box URLs. In PowerShell:

```powershell
$env:BOX_FOLDER_IDS = "123456789"
$env:BOX_DATASET = "box_documents"
uv run python examples/example.py
```

Or use the SDK:

```python
from cognee_community_connector_box import box_source
import cognee

await cognee.remember(
    box_source(folder_ids=["123456789"], resources=("files", "folders", "comments")),
    dataset_name="box_documents",
    self_improvement=False,
    dlt_config={"write_disposition": "merge", "primary_key": "id"},
)
await cognee.recall("What do these documents describe?", datasets=["box_documents"])
```

Re-run after an edit or deletion. Selected folders include descendants. To select only
file text, pass `resources=("files",)`; comments remain separate documents when selected.
Change account, folder selection or resource selection only with a new dedicated dataset.

## Content and deletion

- UTF-8 text files download directly. PDF/Office prose uses Box's
  [extracted-text representation](https://developer.box.com/guides/representations/text).
  Other binaries contribute metadata and comments. Images are not OCR processed.
- Text is capped at 5 MB per file (`max_content_bytes`); oversized or unfinished
  representations fail the sync rather than acknowledge incomplete content.
- A current trashed/deleted item, or a deletion event followed by a 404, removes its
  owned documents from staging; Cognee then reconciles graph/vector memory. A moved
  item confirmed outside the selected folders is removed from this scope.
- Ambiguous 403/404 responses and incomplete listings fail without advancing state
  or publishing a partial extraction. Retry after resolving access/API errors.
  Cached folder descendants are rechecked against their current location, so moved
  files and independently selected folders remain in scope. A comment missing from
  a completed listing is checked individually before retirement. Source-read
  reconciliation removes that owned comment only when its read returns 404 and a
  subsequent fresh read confirms the same file is active, accessible and currently
  within the selected folders. This establishes current source absence even when
  no comment-delete event arrives; a 404 alone does not establish deletion. Parent
  403/404, changed ownership or uncertain scope stops sync with state preserved.
  An inaccessible item in the account-wide event feed can also stop scoped sync.
- [Box user events](https://developer.box.com/guides/events/user-events/for-user)
  can be duplicated/out of order and have finite retention (two weeks to two months).
  Sync regularly. After a gap beyond retention, use a fresh dedicated dataset and
  deliberately retire the old dataset; replaying an expired stream is not a full rescan.

The connector is read-only: it does not change or delete Box content. Test accounts and
LLM/embedding calls still use their configured providers.

## Development

```sh
uv run pytest tests -q
uv run ruff check .
uv run ruff format --check .
```

Offline tests exercise cursor pagination, selection, partial failures, SDK token rotation
and real SQLite dlt merge/deletion. Real Box authorization and Cognee search/deletion
validation require a configured test account; passing offline tests does not prove them.
