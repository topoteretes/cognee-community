# cognee-community-connector-front

A Front data-source connector for [cognee](https://github.com/topoteretes/cognee): sync the
conversations of selected Front inboxes into memory, so an agent can answer "what did we tell
this customer about their refund, and why?" without anyone searching the inbox.

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Each conversation (its
messages and, optionally, its internal comments) becomes one document and goes through
cognee's normal cognify pipeline, via the document-mode marker, the same way the Notion
connector works.

## Requirements

- cognee **1.6.3** or later (below 2): table-scoped document cleanup and per-row node sets.
- Python 3.10 to 3.13.
- A Front API token that can read the inboxes you want to sync.

## Install

```bash
uv pip install cognee-community-connector-front
# or, from this monorepo:
cd packages/connector/front && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_front import front_source

await cognee.remember(
    front_source(token=token, inboxes=["inb_55c8c149", "inb_8c3f2a10"]),
    dataset_name="front",
    primary_key="id",
    write_disposition="merge",  # REQUIRED
    max_rows_per_table=0,
)

answers = await cognee.recall("What did we promise about late refunds?", datasets=["front"])
```

`write_disposition="merge"` is required: later runs only return the conversations that
changed, so the default (`replace`) would wipe the others on the second sync. The token and
settings are arguments only; `examples/example.py` reads them from `FRONT_TOKEN` and
`FRONT_INBOXES`.

| Option | Default | Meaning |
|---|---|---|
| `token` | required | A Front API token. See [Setup](#setup). |
| `inboxes` | required | Ids of the inboxes to sync (`inb_...`). Dropping one later forgets its conversations. |
| `include_comments` | `True` | Include internal comments, labelled `Internal comment`. Switching it re-renders every conversation on the next sync. |
| `include_archived` | `True` | Keep archived conversations. With `False` they are left out, and forgotten once archived. |
| `api_url` | `https://api2.frontapp.com` | Front's API address. https only. |
| `resource_name` | `front` | Table name and key of the sync state: lowercase letters and digits with single underscores, at most 51 characters, so dlt uses it unchanged. Give each Front account its own; a state written for another account is refused. Keep it fixed across runs. |
| `check_active` | `None` | Optional callable checked before every API request, before every retry wait and around every row; raising from it stops the sync. |
| `client` / `service` | `None` | A pre-built client, mainly for tests. |

After a run, `resource.cognee_sync_stats` holds counts only: `scanned` (conversations
written), `deleted`, `skipped` (re-read but unchanged), `failed`, plus `failed_inboxes` or
`failed_rate_limit`.

## Setup

1. **Create an API token** under Settings → Developers → API tokens (company admins), or
   your personal settings for a personal token. Give it read access to the resources that
   hold the inboxes you want: shared inboxes, and private ones if you sync those. The token
   can read whatever those scopes allow, so keep them as narrow as you can.
2. **Find the inbox ids.** Front's app does not show them; run the example without
   `FRONT_INBOXES` and it lists the inboxes the token can see, with their ids:

   ```bash
   FRONT_TOKEN=... uv run python examples/example.py
   ```

   The connector takes ids, not names, because an inbox can be renamed.

## How sync works

| Concern | Behaviour |
|---|---|
| Documents | One per conversation, keyed by its id: the subject, inbox, status, assignee and tags, then each message and comment with who wrote it and when, oldest first. Messages are labelled `Customer (name)` or `Teammate (name)`, comments `Internal comment (name)`. Drafts are left out. Rendering does not depend on the order Front sends things in, so an unchanged conversation keeps its content hash. Each row carries the inbox name as its node set, so recall can be scoped to one inbox, and a `visibility` column: `customer` when the document only holds what the customer saw, `customer_and_internal` when it also holds internal comments. |
| Each sync | Front's conversation listing is not ordered by `updated_at`, and adding or renaming a tag does not move `updated_at`, so every sync pages through the selected inboxes' listings in full (100 conversations a request). Only a conversation whose `updated_at`, tag names, status or inbox changed has its messages and comments read again. |
| What moves `updated_at` | A new message, a deleted message, a comment added, edited or deleted, a subject edit, trash and restore, a merge (on the surviving conversation) and a move. These were checked on a Front account; see the connector issue. |
| Deletes | A trashed conversation stays in the listing with `status="deleted"` and is forgotten; a restore brings it back. A stored conversation missing from a complete listing is looked up on its own: a 404 (deleted for good) or a 301 (merged into another) means it is forgotten, and so does a 200 in an inbox that is not selected (moved out). Forgetting uses the `_deleted` hard-delete marker, which dlt drops on `merge` and cognee's orphan cleanup removes from the graph, vector and relational stores. Front's `/events` is not used: deleting a comment or a conversation erases its earlier events instead of recording a delete. |
| Merges | The 301 of a merged-away conversation is never followed (it would store the survivor's content under the old id). The survivor's `updated_at` moves, so it is read again with the merged messages. |
| Archived | Kept by default; with `include_archived=False` left out and forgotten once archived. |
| Dropped inboxes | Taking an inbox out of `inboxes` forgets its conversations on the next sync. |
| Lost access | A failed inbox listing (403, 404, a 5xx after the retries) never forgets anything. The inbox is retried next run while the others still sync; once it has failed three runs in a row the run raises with the reason. A run where every inbox failed raises at once. |
| Rate limits | Front's limit is per company and depends on the plan, so the client reads `x-ratelimit-remaining` and `x-ratelimit-reset` on every answer and waits for the window to reset when two requests or fewer are left, leaving room for the company's other integrations. |
| Errors | A rejected token (401), an unreachable API or a 200 without JSON (a login or proxy page) end the run at once, and dlt keeps the previous state. 429, 5xx and dropped connections are retried, honouring `Retry-After` up to 30 s; a longer wait stops the run cleanly (`failed_rate_limit`), what was read so far is kept, and the next run resumes. cognee holds a process-wide lock while a dlt source runs, so the connector does not sleep through long waits. |

## Resetting a sync

The per-conversation stamps and fingerprints live in dlt's pipeline state (under
`~/.dlt/pipelines`, or `$DLT_DATA_DIR/pipelines`), not in cognee's data folders. Pruning
cognee does not reset them, so the next sync would only fetch changes. To re-sync from
scratch, delete that pipeline folder as well. The state is kept per `resource_name` and
dataset (`PIPELINE_SCOPE_ATTR`).

## Privacy

Support conversations hold customers' personal data. What the connector does:

**What is read**

- Conversations of the selected inboxes only: subject, status, assignee, tag names, and the
  text of messages and (unless `include_comments=False`) internal comments.
- People are named by name only. Email addresses and phone numbers are never written into a
  document: a customer shows as `Customer (name)`, or just `Customer` when Front only has
  their address, and teammates as their first and last name.
- Never put in a document: attachment contents (an attachment-only message lists the file
  names), drafts, custom fields and message headers.
- Internal comments are labelled `Internal comment`, so an answer can tell what the customer
  was told from what the team said among themselves. Turn them off with
  `include_comments=False` if they should not reach memory at all.
- The connector is read-only (GET requests only). It never sends, edits or deletes anything.

**Where it goes**

- The text above becomes cognee documents. During cognify it is sent to your configured LLM
  and embedding providers, like any document you add to cognee. Use local models if that
  matters to you.
- Anyone who can read the cognee dataset can recall what the synced conversations say. Share
  it only with people who may read those inboxes in Front.
- dlt state stores the account id, the selected inbox names, and per conversation its id,
  `updated_at`, tag names, status, inbox and a short fingerprint. It does not store message
  text.
- Logs and error messages carry counts, HTTP statuses and endpoints with ids replaced, never
  message text or names.
- The token is an argument, is only sent to `api_url`, and is never logged or shown in a
  repr. Surrounding whitespace (a pasted newline) is stripped, and a token with whitespace
  inside is refused without echoing it. Redirects are not followed, and a next-page link to
  another host is refused, so the token never reaches another host.

**How deletion is honoured**

- Trashed, deleted, merged-away and moved-out conversations are forgotten on the **next
  sync**; so is a deleted message or comment (the conversation is re-rendered without it).
- Dropping an inbox from `inboxes` forgets its conversations on the next sync.
- `cognee.forget(dataset=...)` alone is not enough: dlt keeps its staging copy of the rows,
  so the next sync brings forgotten conversations back. Drop the inbox, or delete the
  conversations in Front, and the next sync forgets them.

## Limitations

- Every sync pages through the full listing of each selected inbox (one request per 100
  conversations), because the listing is not ordered by `updated_at` and tag changes do not
  move it. A very large inbox costs that many requests per run.
- Any change re-renders the whole conversation, so a long conversation is cognified again in
  full when it grows.
- A trashed conversation that Front later purges is already forgotten; nothing more happens.
- Only text is ingested: no attachment contents, custom fields or link previews.
- The sync state grows with the number of conversations (a few short fields each).

## Testing

```bash
uv run pytest tests/
```

No Front account is needed. `tests/fake_front.py` is an in-memory Front API with the
behaviour seen on a real account.

- `test_front.py` covers rendering (customer, teammate and internal comment labels; names,
  never addresses; drafts; HTML and entities; time order; tags; node sets), the first sync
  and later runs that only re-read changed conversations, a tag-only change, comments added,
  edited and deleted, a deleted message, trash and restore, permanent delete, merge (the 301
  is never followed), moves out of and between selected inboxes, archived conversations kept
  or left out, `include_comments` switched, a dropped inbox, paging, an inbox that fails or
  keeps failing, rate limits, the HTTP client (retries, dropped connections, long waits,
  redirects, a 200 without JSON, ids kept out of errors, the token, pacing), a sync through the real
  client, config validation, `check_active`, the sync stats, and real dlt merges into SQLite
  (including a failed load and two accounts in one dataset).
- `test_front_forget.py` runs the real cognee pipeline (LLM and embeddings mocked). It checks
  that trashed, deleted, merged-away, moved-out and dropped-inbox conversations lose their
  Data records and leave the graph while an untouched conversation keeps its id, and that a conversation's node set
  names its inbox.
