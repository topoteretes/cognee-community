# cognee-community-connector-egnyte

An Egnyte data-source connector for [cognee](https://github.com/topoteretes/cognee).
It syncs one or more Egnyte folders (plain text, Markdown, CSV, JSON, YAML, PDF and Word files) into memory, incrementally and with forget-on-delete.

It exposes a `dlt` resource you hand to `cognee.remember(...)`, reusing cognee's existing DLT ingestion path.
Files are ingested as **documents**, so each one goes through normal chunking and LLM graph extraction.

## Requirements

- Python 3.10 to 3.14, and cognee 1.6.3 or newer (tested on 1.6.3 and 1.6.5).
  It needs table-scoped document cleanup, so two sources in one dataset never delete each other's files.
  The connector refuses to run on older releases.
- An Egnyte domain and an API key (see **Setup**).

## Install

```bash
uv pip install cognee-community-connector-egnyte
# or, from this monorepo:
cd packages/connector/egnyte && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_egnyte import egnyte_source

await cognee.remember(
    egnyte_source(domain="acme", folder_paths=["/Shared/Projects"]),
    dataset_name="egnyte_projects",
    primary_key="id",
    write_disposition="merge",  # required: makes re-runs incremental and propagates deletes
    max_rows_per_table=0,  # folders often exceed the default 50-row cap
)

answer = await cognee.recall("What did we agree with Acme about the delivery dates?")
```

See `examples/example.py` for the full flow.

### Configuration

Every argument of `egnyte_source(...)` falls back to an environment variable.

| Argument | Environment variable | Notes |
| --- | --- | --- |
| `domain` | `EGNYTE_DOMAIN` | `acme` or `acme.egnyte.com` |
| `folder_paths` | `EGNYTE_FOLDER_PATHS` | Comma-separated, synced recursively, e.g. `/Shared/Projects,/Shared/Legal` |
| `access_token` | `EGNYTE_ACCESS_TOKEN` | Preferred. See **Setup** for how to get one |
| `api_key` | `EGNYTE_API_KEY` | Internal keys only, used when there is no access token |
| `username` / `password` | `EGNYTE_USERNAME` / `EGNYTE_PASSWORD` | Only used when there is no access token |
| `max_file_size_mb` | `EGNYTE_MAX_FILE_SIZE_MB` | Default 25 |
| `requests_per_second` | `EGNYTE_REQUESTS_PER_SECOND` | Default 2, the limit of a default developer key |

Two more arguments have no environment variable:

- `resource_name` (default `egnyte_files`): give each domain or folder set its own name when several Egnyte syncs share one dataset, so their cursors and deletes stay separate.
- `check_active`: an optional callback a host app can pass to re-check authorization before each row.

## Setup

The connector needs an Egnyte access token.
How you get one depends on the type of API key.

### Public application keys (OAuth, recommended)

This is what Egnyte issues for integrations, and what its app certification requires.

1. As an Egnyte admin, open **Apps & Integrations**, search for "API key" and choose **Create API Key**.
   Pick **Public Application** and register the redirect URI `https://localhost/callback` (or your own).
2. Run the login example once and approve access in the browser:

   ```bash
   EGNYTE_DOMAIN=acme EGNYTE_API_KEY=<client id> EGNYTE_API_SECRET=<client secret> \
       python examples/login.py
   ```

   After you approve, the browser lands on the redirect URI.
   That page may not load, which is fine: copy its full URL from the address bar and paste it into the script.
3. Put the printed token in `EGNYTE_ACCESS_TOKEN`.

The same steps work from code with `authorization_url`, `code_from_redirect` and `exchange_code`.

### Internal application keys (password grant)

Internal keys can request a token with a username and password instead:

```python
from cognee_community_connector_egnyte import get_access_token

token = get_access_token("acme", api_key="...", username="you", password="...")
```

Egnyte limits token requests per user per hour, so store the token in `EGNYTE_ACCESS_TOKEN` rather than requesting a new one on every sync.
If you only set `EGNYTE_API_KEY`, `EGNYTE_USERNAME` and `EGNYTE_PASSWORD`, the connector requests a token at the start of each run.

### Then

Set `EGNYTE_DOMAIN` and `EGNYTE_FOLDER_PATHS`, plus your `LLM_API_KEY` like any other cognee run.

The connector only reads.
It never creates, changes or deletes anything in Egnyte.

## How sync and forget-on-delete work

- **First run**: the connector reads the current Events API cursor, then walks every configured folder and ingests each supported file.
  Reading the cursor before the walk means a file changed during a slow first sync is picked up by the next run.
- **Later runs**: the connector reads the file system events since the saved cursor and only re-checks the paths those events touched.
  Unchanged files cost no API calls.
- **Identity**: rows are keyed on the file's `group_id`, which stays the same across new versions, renames and moves.
  A renamed or moved file is updated in place.
- **Deletes**: the connector keeps a small `group_id -> path` map in dlt's resource state.
  When a touched file (or every file under a touched folder) no longer exists in scope, it emits a `_deleted` hard-delete row.
  dlt drops the row on `merge`, and cognee's `orphan_cleanup` removes it from the graph, vector and relational stores.
- **Expired cursor**: Egnyte only keeps a limited window of events.
  If the saved cursor is older than the oldest available event, the connector falls back to a full walk and tombstones every previously known file that is gone.
- **Several syncs**: each `resource_name` has its own cursor and its own cleanup scope.
  Deleting a file under one sync never removes files that came from another, even in the same dataset.
- **Failures**: auth errors, network errors and an exhausted daily quota stop the run without saving the cursor, so the next run retries the same window.
  A single file that can't be parsed is skipped with a warning.

## Rate limits

Default developer keys allow 2 calls per second and 1,000 calls per day.
The connector paces requests and retries the per-second throttle (Egnyte reports it as HTTP 403 with `ERR_403_DEVELOPER_OVER_QPS`).

The daily quota is what to plan around.
A first sync costs roughly one call per folder plus one per supported file, so a folder tree with a few hundred files fits in one day.
For bigger trees, ask Egnyte (api-support@egnyte.com) for a higher limit, or start with a smaller folder and add more paths later.

## Testing

```bash
uv run --group dev pytest tests/
```

The tests fake the Egnyte API, the LLM and the embeddings, so they need no credentials.
They follow what the real API returns: the field names, the events, and the single event Egnyte sends for a whole folder.
The connector was also run against a real Egnyte domain.
It covered PDF and Word extraction, a new file version, renaming a file and a folder, moving a file out of the synced folder, deleting a file, and deleting a folder.
In every case the files and their graph content in cognee followed.
