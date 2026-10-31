# cognee-community-connector-help-scout

A Help Scout data-source connector for [cognee](https://github.com/topoteretes/cognee):
turn your support inbox and help center into memory - "what do customers keep asking about
billing, and what did we answer?".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Conversations
(with their threads) and Docs articles are ingested as **normal documents** (cognee
document-mode), so they go through cognify entity extraction.

## Requirements

* cognee **>= 1.6.1, < 2**. The connector uses `PIPELINE_SCOPE_ATTR`, `guarded_rows` and
  table-scoped document cleanup (`DOCUMENT_SYNC_VERSION >= 1`) and refuses to run on an older core,
  so a sync can never delete another source's data.
* Python 3.10 - 3.13.

## Install

```bash
uv pip install cognee-community-connector-help-scout
# or, from this monorepo:
cd packages/connector/help-scout && uv sync --all-extras
```

## Setup (OAuth 2.0)

1. **Inbox API (conversations):** in Help Scout, open *Your Profile -> My Apps -> Create My App*.
   Copy the **App ID** and **App Secret**. The connector uses the OAuth 2.0 *Client Credentials*
   grant and gets a new token when Help Scout answers `401`. The app acts as the user who created
   it, so it sees that user's inboxes. Already have a token from an *Authorization Code* app? Pass
   `access_token=...` instead (it cannot be renewed).
2. **Docs API (articles, optional):** *Your Profile -> Security and Access -> Docs API* key.
3. The connector only takes credentials as arguments. Read them wherever you keep secrets; the
   example reads these environment variables:

```bash
export HELPSCOUT_APP_ID="..."
export HELPSCOUT_APP_SECRET="..."
export HELPSCOUT_DOCS_API_KEY="..."   # optional, enables articles
export LLM_API_KEY="sk-..."
```

## Usage

```python
import os

import cognee
from cognee_community_connector_help_scout import help_scout_source

source = help_scout_source(
    resource_name="help_scout_acme",  # one per Help Scout account, keep it fixed
    app_id=os.environ["HELPSCOUT_APP_ID"],
    app_secret=os.environ["HELPSCOUT_APP_SECRET"],
    mailbox_ids=[123],  # omit for every inbox
)
await cognee.remember(
    source,
    dataset_name="help_scout",
    primary_key="id",
    write_disposition="merge",  # REQUIRED, see below
)
print(source.cognee_sync_stats)  # integer counts for the run
```

All arguments are keyword-only.

| Argument | Meaning |
| --- | --- |
| `resource_name` | Stable connection name (lowercase, digits, `_`). Prefixes the tables (`<name>_conversations`, `<name>_articles`) and scopes the sync state. Default `help_scout`. |
| `app_id`, `app_secret` | OAuth app credentials. |
| `access_token` | Ready bearer token instead of app credentials. |
| `mailbox_ids` | Only these inboxes. Default: all. Changing it is handled (see below). |
| `include_conversations` | Sync conversations (default `True`). |
| `include_notes` | Include internal notes (default `False`). |
| `include_spam` | Keep spam (default `False`: spam is forgotten). |
| `reconcile` | Look for conversations / articles that vanished without a change (default `True`). |
| `reconcile_every` | Reconcile on every N-th run (default `1`). Raise it on large accounts. |
| `modified_since` | ISO 8601 time that limits the **first** run only. |
| `docs_api_key` | Docs API key; enables articles. |
| `include_articles` | Sync Docs articles. Default: on when a Docs key is given. |
| `collection_ids` | Only these Docs collections. Default: all. |
| `article_status` | `"published"` (default) or `"all"`. |
| `max_rate_limit_wait` | Longest rate-limit wait in seconds (default 120). A longer one stops the run cleanly. |
| `check_active` | Optional host authorization callback, called before each request and around each row. |
| `service` / `client` | Pre-built `httpx.Client`-like object (mainly for tests). |

> **`write_disposition="merge"` is required.** cognee defaults to `replace`, which reloads the
> table each run. An incremental run only sees what changed, so `replace` would forget the rest.

`source.cognee_sync_stats` holds integer counts for the last run: `conversations_synced`,
`conversations_forgotten`, `conversations_thread_requests`, `conversations_reconcile_skipped`,
`articles_synced`, `articles_forgotten`, `failed_rate_limit`, `failed_auth`.

## What a document contains

* **Conversation** (`id = conversation:<id>`): `#number subject`, status, channel, customer name,
  assignee, tags (sorted), created/closed time, and every published thread oldest-first as
  `[customer|reply|chat|phone|forward] Name (time): text`. Internal notes only with
  `include_notes=True`. System "line item" threads and drafts are skipped. HTML becomes text.
* **Docs article** (`id = article:<id>`): name, public URL and the article text.

Only fields that describe the conversation or article itself go into the text, in a fixed order.
Inbox and collection **names** are left out, and tags are sorted: otherwise renaming an inbox or
reordering tags would change the content of every document in it and cognee would re-extract them
all. Volatile fields (preview text, "waiting since", read state) are left out for the same reason.

## How it works

**Incremental sync.** `GET /v2/conversations?status=all&embed=threads&sortField=modifiedAt&sortOrder=asc`,
plus `modifiedSince=<cursor>` after the first run. `status=all` is needed because the default is
active conversations only. The cursor is **Help Scout's clock**, taken from the `Date` header of the
run's first response, minus a 5-minute overlap. If Date is absent, the fallback is the local
run-start time (not the end of a long run). It is kept in dlt resource state.

If a conversation changes while the listing is being paged, it jumps to the end of the
`modifiedAt` order and can push another conversation across a page boundary. That shows up as a
repeated id or changing `totalElements`. Incomplete counts are also rejected. The listing is read
again from page 1 with the same cutoff, including previously seen records in case their content
changed. After three unstable attempts, the cursor stays put for a later retry. These checks reduce
page-shift misses; this API does not provide a snapshot under arbitrary concurrent changes.

**Configuration changes.** `mailbox_ids`, `include_notes` and `include_spam` are stored next to the
cursor. When they change, the next run does a full listing and a reconcile: an inbox added later is
backfilled, and conversations of an inbox removed later are forgotten. The new config is committed
only after its reconcile completes; unstable listings or pending verification batches keep it pending.

**Threads and the rate limit.** Thread bodies come embedded in the list: one request returns up to
25 conversations with their threads. The per-conversation threads endpoint is called only when the
embedded copy can be incomplete: chat conversations (Help Scout truncates embedded chat threads) or
fewer threads than the conversation's `threads` count. A conversation deleted between the list call
and its threads call (`404`) becomes a tombstone instead of failing the run.

**Forget-on-delete.** A conversation in `state == "deleted"`, or moved to spam, becomes a
`{"id", "_deleted": True}` tombstone. On every `reconcile_every`-th run, all conversation ids are
listed (in stable `number` order) to find conversations that just vanished. Page-number paging shifts
when something is deleted mid-listing, so a vanished id is only forgotten when:

1. the listing was complete and consistent (the same `totalElements` on every page, as many ids as
   it said, no repeated ids, no repeated or missing pages), and
2. a re-check of that id (`GET /conversations/{id}`) says it is gone (`404`, deleted, spam, or now
   outside `mailbox_ids`).

At most 200 candidates are verified per run, including after configuration changes. Pending batches
continue on the next run rather than waiting for `reconcile_every`. Checked-live IDs are remembered
within the cycle, so lower IDs cannot starve later deletions. When the cycle ends, the regular cadence
resumes. A per-conversation `403` is **not proof of deletion**: the document stays, an access-denied
counter is incremented, and the next regular cycle can check it again. Listing-level `403` and all
`401` failures still abort the run. Docs articles
follow the same rules (`count` across pages, then `GET /articles/{id}`). dlt removes tombstoned rows
on `merge`. An internal `_sync_order` column makes the last observation in a run win if a
retry emits an updated row or tombstone for the same id, and cognee's orphan cleanup then deletes those documents from the dataset (see
`tests/test_cognee_add.py`).

**Docs articles.** Collections -> article list (the Docs API returns up to 50 per page). An article's
full text is fetched only when its `updatedAt` / `lastPublishedAt` / `status` changed. An empty article
answer fails the run instead of storing a blank document.

**Rate limits and failures.** The Inbox API answers `429` with `X-RateLimit-Retry-After`; the Docs API
with `X-RateLimit-Reset` instead. Waits up to `max_rate_limit_wait` are slept; a longer one **stops the
resource cleanly**: yielded documents and confirmed tombstones are retained. Conversation IDs and
verification progress are saved even on a partial run; that resource's cursor does not advance.
An independent resource may have completed already. `failed_rate_limit` is counted. `5xx` and network
errors are retried with backoff. Except for per-id reconcile `403` handling above, other errors abort. Error
messages and logs carry the API name and status only, never URLs or ids.

## Known gaps

* **Reconcile cost.** A reconcile lists every conversation id: about one request per 25
  conversations, plus one request per vanished id. For 100,000 conversations that is about 4,000
  requests against a 200-400 requests/minute limit. Raise `reconcile_every` on large accounts;
  deleted and spam conversations that show up as a change are still forgotten on every run.
* **State lives in dlt.** The cursor, known ids and article versions are in the dlt pipeline state.
  If that state is lost, the next run is a full backfill, and conversations that were deleted while
  the state was missing are only forgotten once cognee's own cleanup notices them.
* **Clock.** The cursor uses Help Scout's `Date` header; if no response carries one, it falls back to
  local run-start time (still minus the 5-minute overlap). Local clock skew remains a risk then.
* **Listing API.** The v2 list endpoint pages by page number. Help Scout's newer cursor-paged listing
  may reduce page-shift issues. Current retries detect changed counts and repeated ids, but do not
  guarantee snapshot isolation if concurrent changes keep the count unchanged.

## Privacy

Support conversations contain customer data. Nothing is fetched until you run the connector. Limit it
with `mailbox_ids`, keep notes off unless needed, and use a dedicated dataset so you can remove it with
one `cognee.prune`. The connector stores customer **names**, not e-mail addresses.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake Help Scout API (no credentials, no network) and a temporary SQLite destination.
They cover document building (sorted tags, no inbox names), embedded-vs-fetched threads, OAuth renewal,
both rate-limit headers, the clean stop on long waits, the server-clock cursor, a conversation modified
while paging, a delete in the middle of a reconcile listing (60 conversations, never forgetting a live
one), a live conversation missing from the listing, `404` between list and threads, adding and removing
an inbox, `reconcile_every`, two sources in one dataset, a failed run that keeps the cursor, and the
Docs article rules. `tests/test_cognee_add.py` runs the source through `cognee.add` and checks that a
deleted conversation leaves the dataset. The live `cognify` graph test requires BOTH an explicit
`--run-llm-tests` command-line flag and `LLM_API_KEY`. A key discovered by dotenv alone never opts in.
Normal tests have a 60-second timeout; pagination fakes also have strict request budgets.

```bash
# Optional, makes live LLM calls:
uv run --extra dev pytest tests/test_cognee_add.py --run-llm-tests
```
