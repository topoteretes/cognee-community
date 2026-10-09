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
3. Complete Miro's OAuth 2.0 authorization-code flow and copy the resulting
   access token. A non-expiring token is simplest for a scheduled connector;
   an expiring token must be refreshed by the calling application.
4. Export the token:

```bash
export MIRO_ACCESS_TOKEN="your-oauth-access-token"
export MIRO_BOARD_IDS="uXjVExampleBoardId="
export LLM_API_KEY="your-llm-key"
```

Never commit an access token, client secret, or refresh token. Miro documents
the OAuth flow at <https://developers.miro.com/docs/getting-started-with-oauth>.

## Usage

```python
import cognee
from cognee_community_connector_miro import miro_source

await cognee.remember(
    miro_source(board_ids=["uXjVExampleBoardId="]),
    dataset_name="my-miro-boards",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
    self_improvement=False,
)
```

`board_ids` is the clearest way to select content. You can instead pass
`team_id` or `project_id` and omit `board_ids` to sync every visible board in
that scope. Passing an empty collection is rejected so it cannot accidentally
select every visible board. Always use a dedicated dataset so deletion cleanup
cannot affect an unrelated connector.

## Incremental sync and deletion

Miro does not provide a `modified_since` filter or an item deletion feed. The
connector therefore lists the selected boards on every run and keeps each
board's `modifiedAt` value in dlt resource state. Unchanged boards cost only the
listing; a changed board is walked completely.

Frame documents use stable IDs and content hashes. Changed documents are merged
while missing frames, emptied frames, and removed boards produce dlt hard-delete
tombstones. Cognee's orphan cleanup then removes their graph and vector data.
The cursor advances only after every item page was fetched, so a failed or
partial request cannot falsely delete valid memory.

## Why this connector uses REST only

This connector deliberately uses only Miro's REST API. Miro MCP can read
comments, but its agent-oriented tools do not expose the same documented
board `modifiedAt` checkpoint or cursor-paginated, board-wide item inventory
used here for deterministic incremental sync and deletion reconciliation. Its
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

If your credentials are stored in this repository's root `.env`, run these
commands from `packages/connector/miro` with:

```bash
uv run --env-file ../../../.env python examples/example.py
uv run --env-file ../../../.env --with pytest pytest tests/ -q
```

The tests use a fake Miro client and a local SQLite dlt destination. No live
Miro credentials are required; the live endpoint test is skipped by default.

To verify the stable board-wide items endpoint and its cursor pagination
against a board you can access, run:

```bash
export MIRO_ACCESS_TOKEN="your-oauth-access-token"
export MIRO_BOARD_ID="uXjVExampleBoardId="
MIRO_RUN_LIVE_TESTS=1 uv run --with pytest pytest tests/test_miro_live.py -v
```

For a root `.env`, use:

```bash
MIRO_RUN_LIVE_TESTS=1 uv run --env-file ../../../.env --with pytest \
  pytest tests/test_miro_live.py -v
```

The connector calls Miro's documented
[`GET /v2/boards/{board_id}/items`](https://developers.miro.com/reference/get-items-1)
operation without `parent_item_id`, so it receives the complete board inventory
needed for frame grouping and deletion reconciliation. The live test checks
board discovery, `modifiedAt`, and the shape of any returned item records. It
does not print board content or credentials.
The offline integration tests use mocked LLM and embedding calls to verify that
frame documents reach cognee's graph and that deleting one frame removes its
graph content without requiring external model credentials.
