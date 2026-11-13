# cognee-community-connector-freshdesk

A Freshdesk data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync tickets, their conversations and published solution articles into memory,
incrementally, and forget them when they are deleted upstream.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each ticket and each article
becomes a **normal document** in cognee (it flows through chunking and LLM entity
extraction), via cognee's document-mode marker. A ticket's document holds its subject,
status, priority, requester, tags, description and replies, so the same customer and the
same problem are linked across tickets.

## Requirements

- cognee **1.6.3** (pinned). The connector relies on table-scoped document cleanup
  (`DOCUMENT_SYNC_VERSION`), `PIPELINE_SCOPE_ATTR` and `guarded_rows`.
- Python 3.11 to 3.13.
- A Freshdesk account with API access. The free trial works; the **Free plan has no API**.

## Install

```bash
uv pip install cognee-community-connector-freshdesk
# or, from this monorepo:
cd packages/connector/freshdesk && uv sync
```

## Setup

1. Your **domain** is in the address bar: `acme.freshdesk.com/a/...` means `acme`. If the
   bar shows `*.myfreshworks.com`, that is the Freshworks login, not the helpdesk; the
   profile menu's **Go to customer portal** opens `acme.freshdesk.com/support/home`.
2. Your **API key**: profile picture (top right) > **Profile settings** > **View API key**.
   Both the classic keys and the newer `fwapi_...` keys work.
3. Set them, plus your `LLM_API_KEY` like any other cognee run:

   ```bash
   export FRESHDESK_DOMAIN=acme
   export FRESHDESK_API_KEY=your-key
   ```

The API key acts as you: the connector sees what your agent account sees.

## Usage

```python
import cognee
from cognee_community_connector_freshdesk import freshdesk_source

await cognee.remember(
    freshdesk_source(),  # FRESHDESK_DOMAIN and FRESHDESK_API_KEY from the environment
    dataset_name="support",
    primary_key="id",
    write_disposition="merge",  # required: incremental upserts and deletes
    max_rows_per_table=0,  # no row cap; a helpdesk exceeds the default 50
)

answer = await cognee.recall("Which customers had SSO login problems?", datasets=["support"])
```

Run the same `remember` call again whenever you want to sync. See `examples/example.py`
for the full flow.

| Argument | Env var | Default |
|---|---|---|
| `domain` | `FRESHDESK_DOMAIN` | `acme`, `acme.freshdesk.com` or a full URL |
| `api_key` | `FRESHDESK_API_KEY` | – |
| `include_tickets` | – | `True` |
| `include_conversations` | – | `True`: replies are added to the ticket's document |
| `include_private_notes` | – | `False`: agent-only notes stay out unless you opt in |
| `include_articles` | – | `True`: published solution articles |
| `resource_name` | – | `freshdesk`; give each helpdesk its own name when several sync into one dataset |

After a run, `source.cognee_sync_stats` holds counts only (tickets, articles, skipped,
deleted), never subjects or content.

## How sync and forget-on-delete work

These rules come from testing against a live trial helpdesk, not only from the docs.

**Tickets** are incremental.

- Every run lists tickets with `updated_since=<cursor>`, oldest first, with the requester and
  description embedded (that costs the same as the description alone). The first run starts
  from 2000-01-01: Freshdesk's default list only covers the last 30 days.
- `updated_since` is **inclusive**, so the cursor keeps the newest `updated_at` and the ids
  sitting exactly on it. Those are skipped next run unless they changed again, and a ticket
  changed in that same second is still picked up.
- A new reply or note moves the ticket's `updated_at`, so a ticket's conversations are read
  again only when the ticket moved.
- **A deleted or spammed ticket drops out of the normal listing entirely**, even with
  `updated_since`. So every run also lists `filter=deleted` and `filter=spam` since the
  cursor, and forgets those tickets. A **restored** ticket comes back through the normal
  listing.
- Listings stop at page 300; the ticket listing then restarts from the last `updated_at`
  instead of paging further.

**Solution articles** are a snapshot: Freshdesk has no change feed for them.

- Every run walks categories, folders and **every level of subfolders**. A category only lists
  its top-level folders; articles in subfolders are reachable only through
  `/solutions/folders/{id}/subfolders`.
- Only **published** articles are kept. An article is synced again only when its `updated_at`
  moved. An article that is deleted, unpublished or in a deleted folder is forgotten.

**Deletes** are emitted as `{"id": ..., "_deleted": True}` with a `hard_delete` column. dlt
removes them on `merge`, and cognee's orphan cleanup removes them from the graph and vector
stores.

**Errors never delete.** Every API call happens before the first row is yielded and state is
written after the last, so a failed run changes nothing. 401, 403 and other 4xx raise. A 404
is read as "gone" only where it means that: a ticket deleted while its replies are read, or a
folder deleted during the walk.

**Rate limits** are per plan (50 requests a minute on the trial). 429 waits for
`Retry-After`, 5xx backs off, and requests slow down when `X-Ratelimit-Remaining` runs low.
Freshdesk sends that header both as `48` and as `38.0`; both are understood. Every request
has a timeout.

**Changing options** that affect a ticket's document (`include_conversations`,
`include_private_notes`) re-lists every ticket once and reconciles it like a snapshot.
Switching tickets or articles off forgets what was synced from them.

## Privacy

This connector reads customer data. Be clear about what it does:

**What is read**

- Tickets: subject, status, priority, source, type, tags, created and updated time, the
  description, the requester's name, and the text of public replies. Freshdesk also returns
  the requester's email; it is dropped and never stored.
- Private notes only with `include_private_notes=True`.
- Published solution articles: title, location (category and folders), tags and text.
- Not read: attachments, draft articles, contacts and companies beyond the requester's name,
  satisfaction ratings, time entries.
- The connector only sends `GET` requests. It never changes anything in Freshdesk.

**Where it goes**

- The text above becomes cognee documents. During cognify it is sent to your configured LLM
  and embedding providers, like any document you add to cognee. Use local models if that
  matters to you.
- dlt state stores the ticket cursor, ticket ids, article ids with their `updated_at`, and the
  two rendering options. It does not store subjects or content.
- Logs and `cognee_sync_stats` contain counts only.

**How deletion is honoured**

- Deleted and spammed tickets and deleted or unpublished articles are forgotten on the
  **next sync**. Run syncs in the foreground: forgetting happens as part of `remember`.
- To remove everything at once, forget the whole dataset with `cognee.forget`.

## Limitations

- **Unpublished edits.** For a published article that was edited and saved but not
  republished, Freshdesk's API already returns the edited text (still with `status: 2`),
  while the customer portal shows the old one. Nothing in the response marks this, so the
  connector ingests what the API returns.
- **Delete forever.** A ticket that is deleted *and* purged from Trash between two syncs
  disappears from `filter=deleted` too, so an incremental run cannot see it. The next full
  listing (first run, or a changed rendering option) forgets it.
- Agent-only or company-restricted article folders are not filtered out; the key sees what
  your agent sees.
- Attachments are not ingested. Articles are read in the default language only.

## Testing

```bash
uv run pytest tests/
```

No Freshdesk account or API key is needed. `tests/fake_freshdesk.py` is an in-memory
Freshdesk served through a fake HTTP session, so the real client runs too: link-header
paging, retries, `Retry-After`, the rate-limit headers and the page limit. Its behaviour
copies what a live trial helpdesk did.

- `test_freshdesk_sync.py` covers paging, the first-run `updated_since`, rendering, custom
  statuses, private notes in and out, conversations switched off, the inclusive cursor and
  a ticket in the same second, a new reply, deleted, restored and spam tickets, a ticket
  deleted mid-run, the purge limitation and its full-listing fix, the page-limit restart,
  errors that never delete, 5xx backoff, 429 with `Retry-After`, slowing down on a `"2.0"`
  header, subfolders and drafts, article edits, deletes, unpublishing and folder deletes,
  switching tickets or articles off, and two helpdesks in one dataset.
- `test_freshdesk_source.py` covers the resource markers, credentials, refusing an empty
  selection, domain normalization and the header parsing.
- `test_freshdesk_forget.py` runs the real cognee pipeline (LLM and embeddings mocked,
  working with both cognee model variants) and checks that a deleted ticket, a spam ticket
  and a deleted article leave the graph while an untouched ticket stays.
