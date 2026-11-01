# cognee-community-connector-trello

A Trello data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your boards into memory — "ask my boards".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Cards
and board overviews are rendered to markdown and ingested as **normal documents** (they
flow through cognee's cognify entity-extraction pipeline, not the deterministic dlt-row
path), via cognee's document-mode marker.

## Install

```bash
uv pip install cognee-community-connector-trello
# or, from this monorepo:
cd packages/connector/trello && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_trello import trello_source

await cognee.remember(
    trello_source(
        board_ids=["<board id or short link from the board URL>"],
        api_key="...",  # or TRELLO_API_KEY
        token="...",  # or TRELLO_TOKEN
    ),
    dataset_name="boards",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by card id
    max_rows_per_table=0,  # unlimited read-back so deletions reconcile fully
)

answer = await cognee.search(
    query_text="What is blocked on the launch board?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["boards"],
)
```

See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

**Auth:** Trello API key + token (read scope). Create both at
<https://developer.trello.com/power-ups/admin> (or use any existing power-up's key and
generate a token for it) and pass them via `api_key=` / `token=` or the
`TRELLO_API_KEY` / `TRELLO_TOKEN` environment variables. Every request is a `GET`.

**What is ingested:** one document per open card — its description, comments, and
checklists rendered to markdown, tagged with the list it is in — plus, optionally, one
overview document per board (name, description, lists, labels; toggle with
`include_board_documents=False`).

**Incremental sync** follows the board's *actions feed*, per the issue design: cards
carry no reliable updated timestamp, so each run fetches the actions since the last
stored action id and re-syncs the cards those actions touch (comment edits, checklist
updates, moves, renames — all surface as card-affecting actions). Cards that appear in
the board's card listing without corresponding actions (e.g. added before the cursor's
window) are caught by the sweep. Per-board/per-card state lives in dlt's per-resource
state, so re-running `remember` resumes where it left off and re-embeds only the delta.
Documents are also content-hashed, so a re-emit of unchanged content is a no-op
downstream.

**Forget-on-delete:** each run does a cheap id sweep of every fetched board's open
cards and compares it against the ids seen on the previous run. Cards that vanished —
deleted **or archived** — are emitted with an `_deleted` hard-delete marker; dlt removes
those rows on merge, and cognee's existing `orphan_cleanup` purges them from the graph,
vector, and relational stores. Un-archiving re-ingests a card under the same stable id.
Removing a board from `board_ids` tombstones all of its documents on the next sync.

**Failure posture:** a board that fails to fetch is skipped for the run (with a
warning) — its documents are never tombstoned on unseen evidence. A single card that
fails to fetch is skipped and retried on the next run.

## Limitations

- Archiving a card is treated as deletion (it leaves the open-card listing). That is
  the only deletion signal the open-card sweep can see; Trello's actions feed does not
  expose a separate delete event.
- Card documents carry up to 100 comments; older comment history is omitted.
- Syncing several *different* board sets into one dataset? Give each `trello_source(...)`
  call its own `resource_name` so each set keeps its own incremental state and staging
  table.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Trello API (no network, no credentials) and cover document
rendering, the actions-feed incremental cursor, sweep-based forget-on-delete, board
removal, failure handling, and an end-to-end forget-on-delete through a real dlt merge.
