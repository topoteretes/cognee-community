# cognee-community-connector-miro

A frame-aware Miro data-source connector for
[cognee](https://github.com/topoteretes/cognee). It uses dlt's declarative REST
API source for authentication, pagination, retries, and extraction.

Each Miro frame becomes a prose document that flows through cognee's normal
cognify pipeline. Sticky notes, text items, and shape labels are ordered by
their position inside the frame. Items outside a frame are kept in a separate
"Unframed items" document rather than discarded.

## Install

```bash
uv pip install cognee-community-connector-miro

# Or from this repository:
cd packages/connector/miro
uv sync
```

## Miro setup

1. Create a developer team and app in Miro.
2. Add the `boards:read` scope and install the app for the team.
3. Complete Miro's OAuth 2.0 authorization-code flow. Miro recommends expiring
   access tokens; the calling application is responsible for storing the token
   response securely and refreshing it before supplying the current access
   token to this connector.

Pass the token directly when your application already uses a secret manager:

```python
miro_source(access_token=secret_manager.get("miro_access_token"), board_ids=["..."])
```

Alternatively, let dlt resolve it from a secret provider. For local dlt
development, create `.dlt/secrets.toml` under the directory from which you run
the application:

```toml
[sources.miro]
access_token = "your-oauth-access-token"
```

For a deployed process, the equivalent dlt environment variable is:

```bash
export SOURCES__MIRO__ACCESS_TOKEN="your-oauth-access-token"
export MIRO_BOARD_IDS="uXjVExampleBoardId="
```

This package does not load a `.env` file or require one at a particular path.
If your application uses an env-file loader, its location is an application
decision and the file must remain outside version control. Never commit an
access token, client secret, refresh token, or `.dlt/secrets.toml`. Miro
documents the OAuth flow at
<https://developers.miro.com/docs/getting-started-with-oauth>.

## Usage

```python
import cognee
from cognee_community_connector_miro import miro_source

await cognee.remember(
    miro_source(board_ids=["uXjVExampleBoardId="]),
    dataset_name="my-miro-boards",
    primary_key="id",
    write_disposition="replace",
    max_rows_per_table=0,
    self_improvement=False,
)
```

`board_ids` is the clearest way to select content. You can instead pass
`team_id` or `project_id` and omit `board_ids` to sync every visible board in
that scope. Passing an empty collection is rejected so it cannot accidentally
select every visible board. Always use a dedicated dataset so deletion cleanup
cannot affect an unrelated connector.

## Snapshot consistency and deletion

Miro does not provide an item deletion feed. The connector therefore emits a
complete snapshot with `write_disposition="replace"` on every run. Explicit
`board_ids` are fetched directly; team, project, and all-visible scopes use the
paginated board listing.

Frame documents use stable IDs and deterministic content. Missing frames,
emptied frames, and boards confirmed missing with HTTP 404 fall out of the next
snapshot. Cognee's orphan cleanup then removes the corresponding cognee data and
graph content.

For every board, the connector compares `modifiedAt` before and after reading
all item pages. If the board changed during pagination, it discards that read
and retries once. A second change or any non-404 API error aborts the complete
snapshot before yielding rows, so a partial read cannot delete valid memory.

## Why this connector uses REST only

This connector deliberately uses only Miro's REST API. Miro MCP can read
comments, but its agent-oriented tools do not expose the same documented
board `modifiedAt` consistency check or cursor-paginated, board-wide item inventory
used here for deterministic snapshot sync and deletion reconciliation. Its
board content tools return an SVG representation intended for interactive AI
workflows, and Miro recommends REST for repeatable backend integrations.

Combining REST for boards and items with MCP for comments would also require
two authentication models: the connector's OAuth 2.0 access token and a
separate, interactive MCP OAuth 2.1 client session. The package therefore does
not mix the two transports. If Miro exposes comments through REST in the
future, they can be added without changing the connector's authentication or
sync architecture.

## Current Miro API limitations

- Miro's REST API does not support reading comments, although comments were
  included in the original feature request. They cannot be ingested by this
  REST connector until Miro adds that capability.
- Unsupported canvas types such as mind maps, kanban widgets, and tables are
  not converted to text.
- The access token is supplied by the caller. This Path B community connector
  does not add a cognee UI consent screen or credential database.

## Run the example and tests

```bash
uv run python examples/example.py
uv run --with pytest pytest tests/
```

The tests use a fake Miro client and a local SQLite dlt destination. No live
Miro credentials are required; the live endpoint test is skipped by default.

To verify the stable board-wide items endpoint and its cursor pagination
against a board you can access, run:

```bash
export SOURCES__MIRO__ACCESS_TOKEN="your-oauth-access-token"
export MIRO_BOARD_ID="uXjVExampleBoardId="
MIRO_RUN_LIVE_TESTS=1 uv run --with pytest pytest tests/test_miro_live.py -v
```

If you deliberately keep local development values in an ignored file, pass its
location explicitly instead of relying on a repository-specific path:

```bash
MIRO_RUN_LIVE_TESTS=1 uv run --env-file /path/to/private.env --with pytest \
  pytest tests/test_miro_live.py -v
```

The connector calls Miro's documented
[`GET /v2/boards/{board_id}/items`](https://developers.miro.com/reference/get-items-1)
operation without `parent_item_id`, so it receives the complete board inventory
needed for frame grouping and deletion reconciliation. The configured test
board must contain at least one frame with a sticky note, text item, or labelled
shape. The live tests check direct board lookup, `modifiedAt`, item retrieval,
document rendering, and dlt full-snapshot persistence. They do not print board
content or credentials.
The offline integration tests use mocked LLM and embedding calls to verify that
frame documents reach cognee's graph and that deleting one frame removes its
graph content without requiring external model credentials.

For the destructive live check, use a disposable board and a token with
`boards:write`. The test creates two uniquely named frames, verifies a real
shape payload, removes one frame, checks cognee data and graph cleanup, and
removes all remaining test items in `finally`:

```bash
export MIRO_BOARD_ID="uXjVExampleBoardId="
export SOURCES__MIRO__ACCESS_TOKEN="your-read-write-oauth-access-token"
MIRO_RUN_LIVE_WRITE_TESTS=1 uv run --with pytest \
  pytest tests/test_miro_live_write.py -v
```
