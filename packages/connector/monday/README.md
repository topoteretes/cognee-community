# cognee-community-connector-monday

A monday.com data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your boards into memory, incrementally, and forget items when they are archived or
deleted.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each item becomes a **normal
document** in cognee (it flows through chunking and LLM entity extraction), via cognee's
document-mode marker. Items are written in the same shape as the Linear connector in cognee
core, so work items from both trackers read the same way in the graph.

## Requirements

- cognee **1.6.4** (pinned). The connector relies on table-scoped document cleanup
  (`DOCUMENT_SYNC_VERSION`), `PIPELINE_SCOPE_ATTR` and `guarded_rows`.
- Python 3.11 to 3.13.
- A monday.com account (the free plan works) and an API token.
- No other libraries: the client is built on dlt's own REST toolkit.

## Install

```bash
uv pip install cognee-community-connector-monday
# or, from this monorepo:
cd packages/connector/monday && uv sync
```

## Setup

1. In monday.com, click your avatar, then **Developers**, then **API token**, and copy it.
   An OAuth access token from your own monday.com app works the same way; it needs the
   `boards:read` and `updates:read` scopes, plus `workspaces:read` for `list_boards`.
2. Find the ids of the boards you want. The id is the number in a board's URL, or run:

   ```bash
   export MONDAY_API_TOKEN=...
   uv run python examples/list_boards.py
   ```

3. Set your `LLM_API_KEY` (or other LLM settings) like any other cognee run.

## Usage

```python
import cognee
from cognee_community_connector_monday import monday_source

await cognee.remember(
    monday_source(board_ids=[1234567890]),  # MONDAY_API_TOKEN from the environment
    dataset_name="monday",
    primary_key="id",
    write_disposition="merge",  # required: incremental upserts and deletes
    max_rows_per_table=0,  # no row cap; a board exceeds the default 50
)

answer = await cognee.recall("What is stuck right now, and who owns it?", datasets=["monday"])
```

Run the same `remember` call again whenever you want to sync. See `examples/example.py` for
the full flow.

| Argument | Env var | Default |
|---|---|---|
| `board_ids` | `MONDAY_BOARD_IDS` (comma-separated) | required; sub-item boards are added automatically |
| `token` | `MONDAY_API_TOKEN` | required |
| `full_refresh` | – | `False`; `True` re-reads every item (see Limitations) |
| `resource_name` | – | `monday_items`; give each account its own name when several sync into one dataset. A name that already holds another account's sync is refused, so one account can never forget another's items |
| `check_active` | – | optional host checkpoint, run before every API call and every row |

After a run, `source.cognee_sync_stats` holds counts only (`scanned`, `changed`, `deleted`,
`failed`), never names or content.

## What an item looks like in cognee

One document per item, with only what people entered, so an unchanged item always renders
to the same text:

```text
Board: Launch plan
Group: This week
State: Working on it
Assignee: Alex Doe, Sam Roe
Due date: 2026-10-20
Risk: High

# Goal
Ship the beta.
- write tests
- record demo

Updates:
Alex Doe (2026-10-14): Kickoff done
Sam Roe (2026-10-15): Blocked on the API keys
  Reply from Alex Doe (2026-10-15): Asked IT today
```

- **State and Assignee** come from the status column titled "Status" and the people column
  titled Owner, Person or Assignee, like the Linear connector's lines. Any other column is
  written as its own title and value, so any board layout works.
- **Sub-items** live on their own board in monday.com. That board is synced with its parent
  board, and each sub-item gets a `Parent:` line.
- **Formula, mirror and connected-board columns** use their computed value.
- **The item description** (a small monday.com doc) is rendered as markdown.
- **Left out on purpose:** "Last updated" and time-tracking columns, which change without
  anyone editing the item, and file columns, whose text is a list of signed download links.
- **Stable across reorders:** columns are written in column-id order and people, tags and
  dropdowns are sorted, so dragging a column on the board does not re-extract every item.

## How sync and forget-on-delete work

These were checked against a real monday.com account (API version 2026-07, which the client
pins):

- **Change detection.** Every run lists each board's items with cheap stamps: the item's
  `updated_at`, plus the id and `updated_at` of every update and reply. Posting or editing an
  update does **not** move the item's `updated_at` and does **not** appear in the activity log,
  so the update stamps are what catch new, edited and deleted comments. Only items whose
  stamps changed are fetched, a hundred per call.
- **Forget-on-delete.** An item that drops out of a complete listing is looked up by id. It is
  forgotten only when monday.com confirms it is archived or deleted, no longer exists, or moved
  to a board that is not synced. An item moved to another synced board is kept. An item that
  the lookup says is still on the board is kept: one contradicted signal never deletes.
- **Boards.** A board that is archived or deleted upstream, or removed from `board_ids`, has
  all of its items forgotten, sub-items included. monday.com leaves such a board out of a
  normal answer, exactly like a board the token cannot see, so the connector asks with
  `state: all` to tell the two apart.
- Deleted items are emitted as `{"id": ..., "_deleted": True}` with a `hard_delete` column.
  dlt removes them on `merge`, and cognee's orphan cleanup removes them from the graph and
  vector stores.
- **Errors never delete.**
  - A board the token cannot see is reported as failed, never as empty.
  - A failing board keeps its saved state and is retried on the next run, while the other
    boards finish. The run raises only when no board could be synced, or when the token itself
    is refused.
  - monday.com reports most errors inside an HTTP 200 response; every response is checked, so
    an error can never look like an empty result.
- **Rate limits.** When the per-minute complexity budget runs out, monday.com answers HTTP 429
  with an HTML page and no `Retry-After`; the wait is only in its `RateLimit` header. The
  client waits exactly that long, and also honors `retry_in_seconds` on in-body rate limits.
  Listing queries are sized to stay inside the budget.

**API usage.** A sync where nothing changed costs two calls to read the boards, plus one
listing call per board and per sub-item board for every 500 items: 4 calls for one board with
sub-items. Each hundred changed items add one call, and each hundred missing items one lookup.
The free plan allows 1,000 calls a day.

## Privacy

This connector reads work data. Be clear about what it does:

**What is read**

- From the selected boards and their sub-item boards only: item names, group names, column
  titles and values, item descriptions, and updates and replies with their authors' names and
  dates.
- Not read: files and their contents, "Last updated" and time-tracking columns, boards that
  were not selected, and anything outside boards (docs, dashboards, inbox).
- The connector only reads. It never creates, changes or deletes anything in monday.com.

**Where it goes**

- The text above becomes cognee documents. During cognify it is sent to your configured LLM
  and embedding providers, like any document you add to cognee. Use local models if that
  matters to you.
- dlt state stores the account id, board ids and, per item, its id and a short hash of its
  stamps. It does not store names, column values or update text.
- Logs and `cognee_sync_stats` contain counts only, never names, ids or content.
- Your API token is never logged or put in error messages. Keep it out of your code and
  repository; regenerate it on the Developers page if it leaks.

**How deletion is honoured**

- Archived and deleted items are forgotten on the **next sync**. Run syncs in the foreground:
  forgetting happens as part of `remember`.
- Removing a board from `board_ids`, or archiving or deleting it, forgets its items.
- To remove everything at once, forget the whole dataset with `cognee.forget`.

## Limitations

- monday.com gives no signal when **only** an item's description is edited: the item's
  `updated_at` does not move, and description blocks only carry a date. A description is
  re-read whenever its item changes for any other reason, or when you pass
  `full_refresh=True`. A full refresh re-reads every item, but cognee does not re-process
  documents whose text did not change.
- The newest 100 updates of an item are read.
- Files are not ingested.

## Testing

```bash
uv run pytest tests/
```

No monday.com account or API key is needed. `tests/fake_monday.py` is an in-memory
monday.com that behaves like the real one: posting an update does not move the item's
`updated_at`, sub-items sit on their own board, a board the token cannot see is left out of
the answer, and archived and deleted items are still returned by id.

- `test_client.py` covers paging through the cursor, retries on HTTP 429, 5xx and in-body
  rate limits, waiting as long as the `RateLimit` header says, errors inside a 200, a failure
  on a later page, refused and malformed tokens, and `check_active` before every request.
- `test_render.py` covers the Linear-shaped document, volatile columns left out, formula
  values, sub-items, item descriptions, and text that stays the same when columns or people
  are reordered.
- `test_sync.py` covers change detection (column edits, new, edited and deleted updates and
  replies, new items, a full refresh, chunked fetches), forget-on-delete (archived, deleted,
  purged and moved items, archived, deleted and deselected boards, a contradicted listing that
  must not delete), and failures (a lost board, a failing board that must not stop the others,
  a refused token, logs with counts only).
- `test_source.py` covers the factory, its token and board id checks, `list_boards`, and,
  through a real dlt pipeline, a second account under the same `resource_name` being refused
  and two accounts with their own names staying apart in one dataset.
- `test_forget.py` runs the real cognee pipeline (LLM and embeddings mocked) over three syncs:
  deleted items and archived sub-items leave the graph, a new update enters it, and a sync
  where a deletion is the only change still forgets.
