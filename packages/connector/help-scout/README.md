# cognee-community-connector-help-scout

A Help Scout data-source connector for [cognee](https://github.com/topoteretes/cognee):
turn your support inbox and help center into memory - "what do customers keep asking about
billing, and what did we answer?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Conversations (with their threads)
and Docs articles are ingested as **normal documents** (cognee document-mode), so they go through
cognify entity extraction.

## Requirements

cognee **>= 1.4.0** (document-mode: `DOCUMENT_SOURCE_ATTR`).

## Install

```bash
uv pip install cognee-community-connector-help-scout
# or, from this monorepo:
cd packages/connector/help-scout && uv sync --all-extras
```

## Setup (OAuth 2.0)

1. **Inbox API (conversations):** in Help Scout, open *Your Profile -> My Apps -> Create My App*.
   Copy the **App ID** and **App Secret**. The connector uses the OAuth 2.0 *Client Credentials*
   grant to get a token (valid 2 days) and gets a new one by itself when Help Scout answers `401`.
   The app acts as the user who created it, so it sees that user's inboxes.
   - Already have a token from an *Authorization Code* app? Pass `access_token=...` instead
     (it is used as-is and cannot be renewed).
2. **Docs API (articles, optional):** *Your Profile -> Security and Access -> Docs API* key.
   You need the "Docs: Create new, edit settings & Collections" permission to see it.
3. Export the values and your LLM key:

```bash
export HELPSCOUT_APP_ID="..."
export HELPSCOUT_APP_SECRET="..."
export HELPSCOUT_DOCS_API_KEY="..."   # optional, enables articles
export LLM_API_KEY="sk-..."
```

## Usage

```python
import cognee
from cognee_community_connector_help_scout import help_scout_source

await cognee.remember(
    help_scout_source(mailbox_ids=[123]),  # omit mailbox_ids for every inbox
    dataset_name="help_scout",
    write_disposition="merge",  # REQUIRED, see below
)

answer = await cognee.search(
    query_text="What are the most common billing problems and how did we solve them?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["help_scout"],
)
```

| Argument | Meaning |
| --- | --- |
| `app_id`, `app_secret` | OAuth app credentials. Default: `HELPSCOUT_APP_ID` / `HELPSCOUT_APP_SECRET`. |
| `access_token` | Ready bearer token instead of app credentials. Default: `HELPSCOUT_ACCESS_TOKEN`. |
| `mailbox_ids` | Only these inboxes. Default: all. |
| `include_conversations` | Sync conversations (default `True`). |
| `include_notes` | Include internal notes (default `False`). |
| `include_spam` | Keep spam (default `False`: spam is forgotten). |
| `reconcile` | Detect conversations that vanished (default `True`, see cost below). |
| `modified_since` | ISO 8601 time that limits the **first** run only. |
| `docs_api_key` | Docs API key. Default: `HELPSCOUT_DOCS_API_KEY`. |
| `include_articles` | Sync Docs articles. Default: on when a Docs key is set. |
| `collection_ids` | Only these Docs collections. Default: all. |
| `article_status` | `"published"` (default) or `"all"`. |

> **`write_disposition="merge"` is required.** cognee defaults to `replace`, which reloads the
> table each run. An incremental run only sees what changed, so `replace` would forget the rest.

See `examples/example.py` for a runnable script.

## What a document contains

* **Conversation** (`id = conversation:<id>`): `#number subject`, inbox, status, channel,
  customer name, assignee, tags, created/closed time, and every published thread oldest-first as
  `[customer|reply|chat|phone|forward] Name (time): text`. Internal notes only with
  `include_notes=True`. System "line item" threads and drafts are skipped. HTML is converted to text.
* **Docs article** (`id = article:<id>`): name, collection, public URL and the article text.

Volatile fields (preview text, "waiting since", read state) are left out, so they never cause a
re-ingest.

## How incremental sync + forget-on-delete work

**Conversations**

* First run: `GET /v2/conversations?status=all&embed=threads` (all statuses, not only the
  default `active`). Later runs add `modifiedSince=<cursor>`, sorted by `modifiedAt`.
* The cursor is the UTC time the previous run **started**. It is kept in dlt resource state and only
  moves after a fully successful run, so a failed run is retried from the same point.
* **Rate limits.** Thread bodies come embedded in the list (`embed=threads`): one request returns up
  to 25 conversations with their threads. The per-conversation threads endpoint is called only when
  the embedded copy can be incomplete: chat conversations (Help Scout truncates embedded chat
  threads) or when fewer threads came back than the conversation's `threads` count. `429` responses
  wait for `X-RateLimit-Retry-After`.
* **Deletes.** A conversation in `state == "deleted"`, or moved to spam, becomes a
  `{"id", "_deleted": True}` tombstone. Because a deleted conversation can also just disappear,
  `reconcile=True` lists all conversation ids each run (about one request per 25 conversations)
  and tombstones the ones that are gone. dlt removes tombstoned rows on `merge`, and cognee's
  `orphan_cleanup` removes them from the graph and vector stores. With `reconcile=False` a run is
  cheaper but only catches deletes that Help Scout reports as a change; run a reconcile now and then.

**Docs articles**

* Collections -> article list (100 per page). The full article is fetched only when its
  `updatedAt` / `lastPublishedAt` / `status` changed since the last run.
* An article that is deleted or unpublished drops out of the list and is tombstoned.

**Failures.** `5xx` and network errors are retried with backoff. Any other error aborts the run:
no cursor moves and nothing is forgotten, so a partial read can never cause a false deletion.

## Privacy

Support conversations contain customer data. Nothing is fetched until you run the connector. Limit
it with `mailbox_ids`, keep notes off unless needed, and use a dedicated dataset so you can remove it
with one `cognee.prune`. The connector stores customer **names**, not email addresses.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake Help Scout API (no credentials, no network) and a temporary SQLite destination.
They cover HTML conversion, document building, embedded-vs-fetched threads, OAuth token renewal,
rate-limit retries, paging, the `modifiedSince` cursor, forget-on-delete (deleted, spam, vanished
conversations; deleted and unpublished articles) and a failed run that keeps the cursor.
