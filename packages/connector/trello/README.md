# cognee-community-connector-trello

A Trello data-source connector for [cognee](https://github.com/topoteretes/cognee): turn
your boards into memory, so you can ask "what is blocking the export work and who owns it?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Every card and every board
becomes one document that flows through cognee's normal cognify entity extraction, in a
`trello:<board id>` node set so a search can be scoped to one board.

## Install

```bash
uv pip install cognee-community-connector-trello
# or, from this monorepo:
cd packages/connector/trello && uv sync
```

## Setup

1. Trello API keys belong to a Power-Up. Create one at [trello.com/apps/admin](https://trello.com/apps/admin),
   open it, go to the **Trello Auth** tab and choose **Generate a new API Key**.
2. Create a read-only token for your account by opening this link with your key and
   clicking **Allow**:
   `https://trello.com/1/authorize?expiration=30days&scope=read&response_type=token&key=<your key>`
   (`expiration` can also be `1day` or `never`). A token gives access to your whole account,
   so keep it secret. When it expires, the sync raises instead of forgetting anything.
3. Pass both as `api_key=` and `token=`. The example reads them from the environment,
   together with your `LLM_API_KEY` like any other cognee run:

```bash
export TRELLO_API_KEY="..."
export TRELLO_TOKEN="..."
```

## Usage

```python
import os

import cognee
from cognee_community_connector_trello import trello_source

await cognee.remember(
    trello_source(
        board_ids=["<board id>"],
        api_key=os.environ["TRELLO_API_KEY"],
        token=os.environ["TRELLO_TOKEN"],
    ),
    dataset_name="trello",
    primary_key="id",
    write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
    max_rows_per_table=0,
    self_improvement=False,
)

answer = await cognee.recall("What is blocking the export work?", datasets=["trello"])
```

A board id can be the short link in the board's URL (`trello.com/b/<short link>/...`). Run
the same call again to sync only what changed. See `examples/example.py`.

### Choosing what to ingest

| Argument | Default | Meaning |
| --- | --- | --- |
| `board_ids` | all your boards | Boards to sync. A listed board that cannot be read raises. Without it, every board of `workspace_id`, or every board you belong to. |
| `workspace_id` | none | Sync every board of this workspace (id or name). |
| `include_archived` | `False` | Also ingest archived cards, lists and boards, marked as archived. On an old board they can be most of it. |
| `include_comments` | `True` | Add each card's comments to its document. |
| `include_checklists` | `True` | Add each card's checklists to its document. |
| `full_resync` | `False` | Re-render every card and read all comments again. |
| `max_requests` | `1500` | Request budget of one run, checked before each request once a card or board is saved. A bigger account finishes over several runs, which visit the boards in turn. |
| `resource_name` | derived | Names this sync's state and cleanup. Derived from the board or the workspace (plus a short hash when it has capitals, spaces or punctuation); **required** when `board_ids` lists several boards or when neither `board_ids` nor `workspace_id` is given, since two sources sharing one would delete each other's cards. A name you pass must be lowercase snake_case (letters, digits, `_`) of at most 64 characters, so dlt keeps it as it is; a derived name is cut to fit, with the hash keeping cut names apart. |

Changing `include_comments` or `include_checklists` re-renders every card on the next run.

## What is ingested

| Document | Id | Content |
| --- | --- | --- |
| Card | `card:<id>` | list, labels, members, start and due date (`(done)` when completed), archived marker, description, checklists with their items, comments with author and date. The board name stays out, so renaming a board re-processes only the board; the card is in the board's node set |
| Board | `board:<id>` | name, description, lists in order, labels, members, archived marker. Card titles stay out, so a new card does not re-process the board |

Everything is sorted (labels, members, and lists, checklists and items by position, then
id), so the order Trello happens to answer in never changes a document.

**Known gaps:** attachments, custom fields, votes and stickers are not ingested. A comment
edited without any other change on its card shows up on the card's next change, or with
`full_resync=True`. A board in `board_ids` that keeps answering 401 stops every run at that
board once a row was emitted, so the boards listed after it do not sync until it is fixed;
the only trace is `failed_auth` in `cognee_sync_stats`.

## How sync and forget-on-delete work

- **Actions feed.** Cards have no reliable "updated" timestamp, so the board `actions` feed
  drives the sync: its newest action id is the cursor, passed back as `since`, read before
  anything else in the run.
- **Changed boards only.** A board with no new action and the same `dateLastActivity` is
  skipped. Trello leaves some action types out of the feed (comment edits and deletions,
  checklist item and label changes), so a changed board is read again (cards, lists,
  labels, checklists) and compared card by card. Each card's comment count is part of that
  comparison, which also catches added and deleted comments.
- **Comments of changed cards only.** They are read per card, or for the whole board when
  that takes fewer requests (for example on the first sync). The card endpoint returns at
  most 1000 comments, so a card with 1000 or more is always read through the board feed.
  Trello moves a card's comments into the new board's feed when the card changes boards,
  so a moved card is read in full there too. A card
  deleted while the run reads its comments is skipped, and the board is read again next run.
- **Forget-on-delete.** Deleted cards, archived ones (unless `include_archived`), boards
  that are deleted or archived, and boards you stop selecting are removed from memory. A
  removed board is forgotten right after the board listing. Card deletions are decided
  once a pass has visited every selected board; on a big account that pass can span
  several runs cut short by the budget. A card that no board listed during the pass is
  checked with Trello (one request per card) before it is forgotten: it stays when it is
  now on a selected board and not left out as archived, since it moved there after the
  pass read that board, and the next pass syncs it from there. It is forgotten when
  Trello answers 404, or 401 while the token still works (a board you cannot read). A
  run cut short during these checks goes on with the remaining cards. A board discovered through
  `workspace_id` or your board list is forgotten when Trello answers 404 (one that answers
  404 before it was ever synced is skipped), and a board listing that comes back empty
  while boards were synced before forgets nothing until the next run confirms it. A board
  or card answer without its list raises instead of reading as "everything deleted".
- **A run is bounded.** On a rate limit (Trello allows 100 requests per 10 seconds per
  token and 300 per key, and `Retry-After` is honoured up to 60 seconds), an expired token
  after progress, or a spent `max_requests` budget, the run stops cleanly, keeps the boards
  and cards it finished, and reports it in `cognee_sync_stats`: `failed` is set on every
  cut, since the sync is incomplete, with `failed_rate_limit`, `failed_auth` or
  `failed_budget` saying why; all of these keys are present on every run, `0` when unused.
  The next run starts after the last board it finished. Any other error raises, and dlt
  keeps the previous state.
- **Host checks and cleanup.** `check_active` is called before every request, and the
  client built from `api_key` and `token` also calls it before and after each retry wait,
  so revoked access stops a run even mid-retry. That client is closed at the end of every
  run, also on errors; a `service` you pass in stays yours to close.

**Requests per run:** one `/members/me` check, then two per unchanged board (the feed and
the board itself). A changed board adds two (its cards and checklists) plus the comment
reads of its changed cards, and the end of a pass one per card it is about to forget.

## Privacy

This reads your boards' content, including comments. Nothing is fetched until you call
`remember`, and anyone with read access to the target dataset can read what was ingested,
so keep Trello in its own dataset. The key and token are sent in the `Authorization`
header, never in URLs, and are never written into documents, state or error messages.

## Testing

```bash
uv run pytest tests/
```

The tests need no Trello account. They run the client against a mocked HTTP transport,
check card and board rendering (including ordering), drive the sync against a fake Trello
API through a real dlt pipeline (feed cursor, changes the feed leaves out, comment reads,
moved cards, include flag changes, deletions, unreadable boards, stopped runs, two sources
in one dataset), and run `cognee.remember` end to end to prove a deleted card leaves the
graph.
