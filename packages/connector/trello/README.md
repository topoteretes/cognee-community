# cognee-community-connector-trello

A Trello data-source connector for [cognee](https://github.com/topoteretes/cognee): turn
your boards into memory, so you can ask "what is blocking the export work and who owns it?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Every card becomes one
document (list, labels, members, dates, description, checklists, comments) and every board
one more (description, lists, labels, members), all flowing through cognee's normal
cognify entity extraction.

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
   so keep it secret.
3. Export both, together with your `LLM_API_KEY` like any other cognee run:

```bash
export TRELLO_API_KEY="..."
export TRELLO_TOKEN="..."
```

## Usage

```python
import cognee
from cognee_community_connector_trello import trello_source

await cognee.remember(
    trello_source(board_ids=["<board id>"]),  # credentials from the TRELLO_* env vars
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
| `board_ids` | all your boards | Boards to sync. Without it, every board of `workspace_id`, or every board you belong to. |
| `workspace_id` | none | Sync every board of this workspace (id or name). |
| `include_archived` | `True` | Keep archived cards, lists and boards, marked as archived. `False` forgets them. |
| `include_comments` | `True` | Add each card's comments to its document. |
| `include_checklists` | `True` | Add each card's checklists to its document. |
| `full_resync` | `False` | Read every board's comments again, whatever the feed says. |
| `resource_name` | `trello_cards` | Staging table. Use a different name per sync scope that shares a dataset. |

## How sync and forget-on-delete work

- **Actions feed.** Cards have no reliable "updated" timestamp, so the board `actions`
  feed drives the sync: its newest action id is the cursor, passed back as `since`.
- **Snapshot.** Trello leaves some action types out of the feed: comment edits and
  deletions, checklist item changes and label changes only reach webhooks. So every run
  also reads one snapshot of each board (a single request with its cards, lists, labels,
  members and checklists) and compares each card with the last run. Only cards whose
  document changed are processed again.
- **Comments.** Comments are the expensive part, so they are read again only when the
  feed has new actions, the board's `dateLastActivity` moved, or a card changed. A
  comment edited or deleted with neither of those signals shows up on the card's next
  change, or with `full_resync=True`.
- **Forget-on-delete.** A card deleted or moved to another board, a board deleted or no
  longer visible to your token, and a board you stop selecting are removed from memory
  on the next sync. With `include_archived=False`, archiving does the same. Archiving a
  list or board does not archive its cards in Trello's API, so a card counts as archived
  if the card, its list or its board is archived.
- **Failures.** The token is checked first, so a bad key or token raises instead of
  looking like every board was deleted. Other errors abort the sync after retries
  (429 and 5xx), and dlt keeps the previous state.

Trello allows 100 requests per 10 seconds per token and 300 per key. A board with no
changes costs two requests per sync, and a changed board adds one request per 1,000
comments.

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
check card and board rendering, drive the sync against a fake Trello API through a real
dlt pipeline (feed cursor, changes the feed leaves out, deletions, gone boards, failed
runs), and run `cognee.remember` end to end to prove a deleted card leaves the graph.
