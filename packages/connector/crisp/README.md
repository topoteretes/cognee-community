# cognee-community-connector-crisp

A Crisp data-source connector for [cognee](https://github.com/topoteretes/cognee): sync a
website's chat conversations into memory, so an agent can answer "what did we tell customers
about failed payments last week?" without anyone scrolling through the inbox.

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Each conversation becomes one
document and goes through cognee's normal cognify pipeline, via the document-mode marker,
the same way the Notion connector works.

## Requirements

- cognee **1.6.3** or later (below 2): table-scoped document cleanup.
- Python 3.10 to 3.13.
- A Crisp token (identifier and key) that can read the website's conversations.

## Install

```bash
uv pip install cognee-community-connector-crisp
# or, from this monorepo:
cd packages/connector/crisp && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_crisp import crisp_source

await cognee.remember(
    crisp_source(website_id=website_id, identifier=identifier, key=key),
    dataset_name="crisp",
    primary_key="id",
    write_disposition="merge",  # REQUIRED
    max_rows_per_table=0,
)

answers = await cognee.recall("Why did payments fail last week?", datasets=["crisp"])
```

`write_disposition="merge"` is required: later runs only return the conversations that
changed, so the default (`replace`) would wipe the others on the second sync. Credentials and
settings are arguments only; `examples/example.py` reads them from `CRISP_*` environment
variables.

| Option | Default | Meaning |
|---|---|---|
| `website_id` | required | The website's id (a UUID): Settings → Website settings → Setup instructions. |
| `identifier`, `key` | required | Your Crisp token. See [Setup](#setup). |
| `tier` | `website` | The token's tier: `website`, or `plugin` for a Marketplace plugin token. |
| `include_notes` | `True` | Include internal notes, labelled `Internal note`. Switching it re-renders every conversation. |
| `visitor_fields` | `("nickname", "segments")` | Visitor details put in each document's header, out of `nickname`, `segments`, `country`, `city`, `email`, `phone` and `ip`. Email, phone and IP are personal data and only included when listed. Without `nickname`, messages are labelled `Visitor` with no name. Changing it re-renders every conversation. |
| `recent_days` | `3` | Conversations active in this many days are read again every run, so an edited or removed message there is picked up on the next sync. |
| `rotation` | `50` | Older conversations read again per run, in turn, to pick up late edits and removals. `0` turns it off. |
| `max_requests` | `2000` | Requests one run may make. Past it the run stops cleanly and the next one resumes. Crisp allows a website token 10,000 a day. |
| `resource_name` | `crisp` | Table name and key of the sync state: lowercase letters and digits with single underscores, at most 51 characters, so dlt uses it unchanged. Give each website its own; a state written for another website is refused. Keep it fixed across runs. |
| `check_active` | `None` | Optional callable checked before every API request, before every retry wait and around every row; raising from it stops the sync. |
| `client` / `service` | `None` | A pre-built client, mainly for tests. |

After a run, `resource.cognee_sync_stats` holds counts only: `scanned` (conversations
written), `deleted`, `skipped` (re-read but unchanged), `failed`, plus `failed_rate_limit`.

## Setup

1. Get a token that can read conversations, following Crisp's REST API authentication
   guide: a website token (`tier="website"`, the default) or a Marketplace plugin token
   (`tier="plugin"`). It needs read access to conversation sessions and messages. It is an
   identifier and a key; keep the key like a password.
2. Find the website id under Settings → Website settings → Setup instructions (the
   `CRISP_WEBSITE_ID` in the chatbox snippet).

## How sync works

| Concern | Behaviour |
|---|---|
| Documents | One per conversation, keyed by its session id: the subject (or "Conversation with <nickname>"), state and the selected `visitor_fields` (nickname and segments by default), then each message with who wrote it and when, oldest first. Messages are labelled `Visitor (name)` or `Operator (name)`, notes `Internal note (name)`. State changes and other `event` messages are left out; files are listed by name; edited messages appear in their current form. Rendering does not depend on the order Crisp sends messages in, so an unchanged conversation keeps its content hash. |
| Each sync | The full conversation list is read every run, empty conversations included (`include_empty=1`). A conversation's history is read again when its `updated_at`, state or visitor details changed. |
| What `updated_at` misses | Checked on a Crisp workspace: `updated_at` only moves when a new message arrives. Editing or removing a message, editing a note, renaming the visitor and resolving do not move it (state and visitor details are visible in the list, so those are caught). So each run also reads again every conversation active in the last `recent_days` days, plus `rotation` older ones in turn; unchanged ones are skipped by fingerprint. An edit or removal in a recent conversation lands on the next sync; in an older one, within one rotation (conversations ÷ `rotation` runs). |
| Deletes | A conversation missing from the complete list is checked with `HEAD`; a 404 (on a conversation that was synced before) means it was removed, and it is forgotten. A conversation removed while a sync runs (its history answers 404) is skipped that run and forgotten on the next. A conversation whose messages were all removed is forgotten too. Forgetting uses the `_deleted` hard-delete marker, which dlt drops on `merge` and cognee's orphan cleanup removes from the graph, vector and relational stores. A removed message leaves its conversation's document when the conversation is read again. |
| Limits | Each run counts its requests against `max_requests` and stops cleanly past it (`failed_rate_limit`); what was read is kept and the next run resumes. Crisp reports no request counters, only `x-rate-limit-watermarks` (`g=low;u=low;r=low`); when one reads `high`, the client pauses 10 s before the next request. 420/429, 5xx and dropped connections are retried, honouring `Retry-After` up to 30 s; a longer wait stops the run cleanly. cognee holds a process-wide lock while a dlt source runs, so the connector does not sleep through long waits. |
| Errors | A rejected token (401/403), a redirect or an answer without `data` stop the run, and dlt keeps the previous state. |

## Resetting a sync

The per-conversation stamps, fingerprints and rotation position live in dlt's pipeline state
(under `~/.dlt/pipelines`, or `$DLT_DATA_DIR/pipelines`), not in cognee's data folders.
Pruning cognee does not reset them. To re-sync from scratch, delete that pipeline folder as
well. The state is kept per `resource_name` and dataset (`PIPELINE_SCOPE_ATTR`).

## Privacy

Chat conversations hold visitors' personal data. What the connector does:

**What is read**

- Conversations of the given website: messages, notes (unless `include_notes=False`), the
  state, a subject when there is one, and the visitor details in `visitor_fields` (by
  default the nickname and segments).
- The visitor's email address, phone number, IP address and location only reach a document
  when listed in `visitor_fields`. Never put in a document: file contents (a file shows as
  its name) and state-change events. A nickname that is an email address or phone number is
  left out.
- Internal notes are labelled `Internal note`, so an answer can tell what the visitor was
  told from what the team noted. Turn them off with `include_notes=False` if they should not
  reach memory at all.
- The connector is read-only (GET and HEAD requests). It never sends or changes anything.

**Where it goes**

- The text above becomes cognee documents. During cognify it is sent to your configured LLM
  and embedding providers, like any document you add to cognee. Use local models if that
  matters to you.
- Anyone who can read the cognee dataset can recall what the synced conversations say. Share
  it only with people who may read the Crisp inbox.
- dlt state stores the website id, the rotation position, and per conversation its session
  id and two short fingerprints. It does not store message text.
- Logs and error messages carry counts, HTTP statuses and endpoints with ids replaced, never
  message text or names.
- The identifier and key are arguments, only sent to `api.crisp.chat`, and never logged or
  shown in a repr. Redirects are not followed.

**How deletion is honoured**

- A removed conversation is forgotten on the **next sync**; so is a conversation whose
  messages were all removed.
- A removed or edited message in a conversation active in the last `recent_days` days is
  reflected on the next sync; in an older conversation, within one rotation.
- `cognee.forget(dataset=...)` alone is not enough: dlt keeps its staging copy of the rows,
  so the next sync brings forgotten conversations back. Remove the conversation in Crisp, and
  the next sync forgets it.

## Limitations

- An edit or removal in an old conversation is only seen when the rotation reaches it, since
  Crisp does not move `updated_at` for it. Raise `rotation` to shorten the wait, at the cost
  of more requests.
- Every run reads the full conversation list (one request per page), plus two requests per
  conversation it reads again.
- A conversation with a very long history is read in full each time it is read again.
- Only text is ingested: no file contents, and rich messages (pickers, carousels) only as
  their text.

## Testing

```bash
uv run pytest tests/
```

No Crisp account is needed. `tests/fake_crisp.py` is an in-memory Crisp API with the
behaviour seen on a real workspace.

- `test_crisp.py` covers rendering (visitor, operator and internal note labels; names, no
  email addresses, phone numbers or IPs unless asked for; `visitor_fields`; state events
  left out; files; time order), the first sync and later runs that only re-read changed
  conversations, edits and removals that do not move `updated_at` (through the recent window
  and the rotation), resolving and renaming, a conversation that becomes empty, a removed
  conversation, one removed mid-sync, empty conversations, `include_notes` switched, paging
  of the list and of messages, the request budget and rate limits, the HTTP client (Basic
  auth and tier, HEAD 404, retries, the watermark pause, redirects, an answer without data,
  the key), a sync through the real client, config validation,
  `check_active`, the sync stats, and real dlt merges into SQLite (including a failed load
  and two websites).
- `test_crisp_forget.py` runs the real cognee pipeline (LLM and embeddings mocked). It checks
  that a removed conversation and a removed message leave the graph while the rest stays.
