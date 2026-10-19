# cognee-community-connector-figma

A Figma data-source connector for [cognee](https://github.com/topoteretes/cognee): sync your
design files and the discussions on them into memory, so you can ask "why did we change the
checkout flow?".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Every file and
every comment thread is rendered to text and ingested as a **normal document**. It goes through
cognee's cognify entity-extraction pipeline, not the deterministic dlt-row path. Re-syncs only
fetch files that changed, and files or comments deleted in Figma are **forgotten** on the next
sync.

All Figma I/O goes through dlt's REST API toolkit. The folder and file listings, file metadata,
node trees and comments are declarative `rest_api` resources; only the subfolder walk, whose
depth is known at run time, uses the same client config through `RESTClient`. Auth and
`Retry-After`-aware retries come from dlt.

## What gets ingested

A Figma file is a node canvas, not prose, so the connector keeps only the text a knowledge graph
can use:

| Document | Contents |
| --- | --- |
| One per file (`file:<key>`) | File name and type, folder, page names, frame / section / component names as headings, and the text of every text layer (plus FigJam stickies, shapes and connector labels) |
| One per comment thread (`comment:<key>:<id>`) | The root comment and its replies in time order, each with author and timestamp, where the thread is pinned (`Page / Frame`), and whether it is resolved |

A file and its comment threads share one node set, `figma:<file name> (<file key>)`, so they can be
searched together. Geometry, styles, images and vector data are never fetched. Hidden layers and Figma's default
layer names ("Frame 12", "Group 3") are skipped as noise. Comments are usually the most valuable
part: that is where design decisions get argued and settled.

## Requirements

Requires cognee 1.6.4, which has the table-scoped document cleanup and per-row node sets
(`DOCUMENT_SYNC_VERSION` 2) the source relies on. `figma_source` refuses to start on an older build.
The optional integration also needs the `OAuthIntegration` resource-picker contract (cognee 1.6.2+).

## Install

```bash
uv pip install cognee-community-connector-figma
# or, from this monorepo:
cd packages/connector/figma && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_figma import figma_source

await cognee.remember(
    figma_source(access_token, folder_ids=["<folder id>"]),  # or FIGMA_ACCESS_TOKEN from env
    dataset_name="figma",
)

answer = await cognee.search(
    query_text="What did we decide about the onboarding copy?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["figma"],
)
```

Leave `write_disposition` at its default (`"replace"`): the source is a full snapshot, and that is
what makes deletions propagate.

Choose what to ingest (at least one is required; they combine):

| Argument | Effect |
| --- | --- |
| `team_ids=[...]` | Every folder of these teams, subfolders included. The team id is in the team page URL after `/team/`; Figma has no API that lists a user's teams. |
| `folder_ids=[...]` | These folders (called "projects" in older Figma) and their subfolders. The id is in the folder URL after `/project/` or `/folder/`. |
| `file_keys=[...]` | These files. The key is in the file URL after `/design/`, `/board/` or `/file/`. |
| `include_comments=False` | Skip comment threads. |

An explicitly empty selection (`folder_ids=[]`) is valid: it yields an empty snapshot, so
everything synced earlier into that dataset is forgotten. Use one Figma source per dataset; two
sources loading into the same dataset would replace each other's snapshot.

See `examples/example.py` for the full flow.

## Setup: Figma OAuth app

The connector authenticates with an OAuth2 access token (`Authorization: Bearer`), as the issue
for this connector asked. For a quick local try you can use a personal access token instead
(`personal_access_token=` or `FIGMA_PERSONAL_ACCESS_TOKEN`, sent as `X-Figma-Token`).

1. In Figma, open the [developer apps page](https://www.figma.com/developers/apps) and create a new
   app. Associate it with your team or organization.
2. **Make it a private app** if you want to sync by team or folder. Figma does not let public
   OAuth apps use the `folders:read` scope, which listing folders needs. A public app can still
   sync `file_keys`.
3. Add your redirect URL (for the cognee integration below:
   `https://<your cognee host>/api/v1/integrations/figma/callback`).
4. Under **OAuth scopes**, select:

   | Scope | Why |
   | --- | --- |
   | `file_content:read` | Read file node trees (text layers, frame names) |
   | `file_comments:read` | Read comment threads |
   | `folders:read` | List folders, subfolders and their files (private apps only) |
   | `file_metadata:read` | Read file metadata, only needed for `file_keys` |
   | `current_user:read` | Identify who connected, only needed for the cognee integration |

5. Copy the **client ID** and **client secret**.
6. Get an access token either through the cognee integration below, or with Figma's standard
   authorization-code flow (`https://www.figma.com/oauth` → `POST
   https://api.figma.com/v1/oauth/token`), then export it with your `LLM_API_KEY` as for any other
   cognee run:

   ```bash
   export FIGMA_ACCESS_TOKEN="<access token>"
   ```

Figma access tokens last 90 days; the integration below refreshes them automatically.

## Connect from cognee (OAuth + folder picker)

The package also ships `FigmaIntegration`, an implementation of cognee's `OAuthIntegration`
contract. Registered with the cognee API, it serves the whole connect flow through cognee's
generic routes, so users connect with OAuth and choose folders without handling tokens:

| Route | What it does |
| --- | --- |
| `POST /api/v1/integrations/figma/authorize` | Returns the Figma consent-screen URL to send the user to |
| `GET /api/v1/integrations/figma/callback` | Exchanges the code and stores the token encrypted |
| `GET /api/v1/integrations/figma/resources` | Lists the folders of the configured teams |
| `PUT /api/v1/integrations/figma/resources` | Saves the folder selection (`{"resource_ids": [...]}`) |
| `GET /api/v1/integrations/figma/connection` | Shows the connection and its last sync status |
| `POST /api/v1/integrations/figma/sync` | Syncs the selection into the account's own dataset |
| `DELETE /api/v1/integrations/figma/connection` | Disconnects (optionally deleting the dataset) |

1. Register it in the process that serves cognee's API:

   ```python
   import cognee_community_connector_figma.register  # noqa: F401
   from cognee.api.client import app  # serve this app, e.g. with uvicorn
   ```

2. Configure it (plus cognee's own `INTEGRATION_CREDENTIALS_KEY` for token encryption):

   ```bash
   export FIGMA_CLIENT_ID="<client id>"
   export FIGMA_CLIENT_SECRET="<client secret>"
   export FIGMA_REDIRECT_URI="https://<your cognee host>/api/v1/integrations/figma/callback"
   export FIGMA_FRONTEND_BASE_URL="https://<your cognee frontend>"
   export FIGMA_STATE_SECRET="<random string that signs the OAuth state>"
   export FIGMA_TEAM_IDS="<team id>,<team id>"  # teams whose folders the picker offers
   ```

A new connection starts with nothing selected. The selection follows the picker's three states:
`null` syncs every folder of `FIGMA_TEAM_IDS`, `[]` syncs nothing (and forgets what was synced),
and a list syncs those folders. Figma documents no token-revoke endpoint, so disconnecting revokes
the local credential only; users can also remove the app under **Settings → Security → Connected
apps** in Figma.

## How sync and forget-on-delete work

- **Full snapshot.** Figma has no change or delete feed, so each run is a complete snapshot
  (`write_disposition="replace"`): staging is rewritten with exactly the files and threads
  currently in the selection.
- **Incremental fetch.** The expensive call is `GET /v1/files/:key` (the whole node tree, in
  Figma's strictest rate-limit tier). Each run lists the selection, and only files whose
  `last_modified` stamp moved since the last run are fetched again; the file's `version` is
  recorded with it. (Figma's listings carry the modification time, not the version number.)
  Unchanged files are re-emitted from a render cache kept in dlt's **source** state (`replace` resets
  resource state every run, source state survives). The cache holds only what the node tree
  contributes; title and folder come from the fresh listing every run, so a rename or a folder move
  shows up without a re-fetch. Comments do not move a
  file's stamp, so they are re-read every run with one request per file. Unchanged rows keep a
  stable content-hash `data_id`, so cognee does not re-ingest or re-cognify them, and an edit that
  only moves layers re-renders to the same text.
- **Forget-on-delete.** A deleted, trashed or moved-out file drops out of the listing (or answers
  `404`), and a deleted comment or reply drops out of its file's comments. Either way it is absent
  from the snapshot, and cognee's `orphan_cleanup` removes it from the graph and vector stores.
  Deselecting a folder forgets its files the same way.
- **Failure safety.** Errors are never swallowed: a `401`/`403`, retries exhausted on a
  `429`/`5xx`, or a network failure. The run aborts before dlt commits the new snapshot and state,
  so nothing is forgotten and the next run starts over. Only `404` is treated as "gone".

**Rate limits.** `GET /v1/files/:key` is in Figma's strictest tier (Tier 1), and the allowance
depends on the seat of the user the token belongs to: Dev and Full seats get 10 to 20 requests a
minute depending on the plan, while View and Collab seats get about 20 a month. Use a token from a
Full or Dev seat. A first sync costs one request per file; later syncs cost one only for files that
changed (plus one cheap comments request per file). `429` responses are retried, honouring
`Retry-After`; if Figma's allowance is exhausted for longer than the retries wait, the run aborts
without forgetting anything and the next run picks up from the cache.

**Limitations.** Figma cannot list a user's teams, so team ids must be supplied. Text inside
images, and text in components that live in another (library) file, is not ingested. Large files
are fetched whole on change, since Figma cannot return only text layers.

## Testing

```bash
uv run pytest tests/
```

The tests need no Figma account. Figma is served by an in-memory fake mounted as a `requests`
transport adapter, so the real dlt auth, `rest_api` and retry code runs. They cover:

- rendering of node trees (pages, frames, text, FigJam, hidden layers, very deep trees) and
  comment threads
- selection by team (with nested folders), folder and file key, with both token types
- the incremental cursor: unchanged files are not fetched, edited ones are, comments are read
  every run, and a rename or folder move shows up without a re-fetch
- file, folder, comment and reply deletion, deselection, and files deleted mid-run
- `Retry-After` handling, and safety on a failed run
- a real `dlt` pipeline proving `replace` drops deleted rows while the cache survives in state
- the OAuth integration: authorize URL, Basic-auth code exchange, installation parsing, folder
  picker, stored-selection sync and token refresh
- an end-to-end `cognee.add` + `cognify` run (LLM mocked) where deleted files and comments leave
  the graph, an unchanged re-sync makes no LLM call, and a file and its comments share a node set
- a smoke run of the packaged `examples/example.py`
