# cognee-community-connector-zoho-desk

A Zoho Desk data-source connector for [cognee](https://github.com/topoteretes/cognee): sync
support tickets (with their conversations) and published knowledge-base articles into memory,
so an agent can answer "have we seen this error before, and what fixed it?" from past tickets.

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Each ticket and each article
becomes one document and goes through cognee's normal cognify pipeline, via the
document-mode marker, the same way the Notion connector works.

## Requirements

- cognee **1.6.3** or later (below 2): table-scoped document cleanup and per-row node sets.
- Python 3.10 to 3.13.
- A Zoho Desk account, and a Zoho Self Client (no redirect server needed). See
  [Setup](#setup).

## Install

```bash
uv pip install cognee-community-connector-zoho-desk
# or, from this monorepo:
cd packages/connector/zoho-desk && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_zoho_desk import zoho_desk_source

await cognee.remember(
    zoho_desk_source(
        data_center="com",  # the end of your Zoho address: com, eu, in, com.au, ...
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
    ),
    dataset_name="zoho_desk",
    primary_key="id",
    write_disposition="merge",  # REQUIRED
    max_rows_per_table=0,
)

answers = await cognee.recall("How did we fix login errors last time?", datasets=["zoho_desk"])
```

`write_disposition="merge"` is required: later runs only return what changed, so the default
(`replace`) would wipe the rest on the second sync. Credentials and settings are arguments
only; `examples/example.py` reads them from `ZOHO_DESK_*` environment variables.

| Option | Default | Meaning |
|---|---|---|
| `data_center` | `com` | Where the account lives: `com`, `eu`, `in`, `com.au`, `jp`, `ca`, `sa` or `com.cn`. |
| `client_id`, `client_secret`, `refresh_token` | required | From a Zoho Self Client. See [Setup](#setup). |
| `org_id` | `None` | The Zoho Desk organization id. Only needed when the account belongs to more than one. |
| `departments` | all | Ids of the departments to sync. A ticket or article that moves out of them is forgotten, and changing the list re-reads every ticket. |
| `include_comments` | `True` | Include ticket comments, labelled `Internal comment` (or `Comment` when public). Switching it re-reads every ticket. |
| `include_articles` | `True` | Include published knowledge-base articles. Turning it off forgets them. |
| `resource_name` | `zoho_desk` | Table name and key of the sync state: lowercase letters and digits with single underscores, at most 51 characters, so dlt uses it unchanged. Give each organization its own; a state written for another one is refused. Keep it fixed across runs. |
| `check_active` | `None` | Optional callable checked before every API request, before every retry wait and around every row; raising from it stops the sync. |
| `client` / `service` | `None` | A pre-built client, mainly for tests. |

Row ids are `ticket:<id>` and `article:<id>`. After a run, `resource.cognee_sync_stats` holds
counts only: `scanned` (documents written), `deleted`, `skipped` (re-read but unchanged),
`failed`, plus `failed_rate_limit`.

## Setup

1. Open the Zoho API console for your data center (`https://api-console.zoho.com`, or
   `.eu`, `.in`, ...), choose **Add Client → Self Client**, and note the **Client ID** and
   **Client Secret**.
2. Under **Generate Code**, enter these scopes, a description and a duration, and create the
   code (it is valid for the minutes you chose):

   ```
   Desk.tickets.READ,Desk.basic.READ,Desk.search.READ,Desk.articles.READ,Desk.contacts.READ,Desk.settings.READ
   ```

3. Exchange the code for a refresh token once (replace the domain for your data center):

   ```bash
   curl -X POST "https://accounts.zoho.com/oauth/v2/token" \
     -d grant_type=authorization_code -d client_id=... -d client_secret=... -d code=...
   ```

   The answer's `refresh_token` does not expire until you revoke it. Keep it like a
   password: it can read every ticket the user can.
4. The scopes are read-only. The user who created the Self Client should only see the
   departments you want synced, or pass `departments=`.

## How sync works

| Concern | Behaviour |
|---|---|
| Documents | One per ticket: the number and subject, department, status, priority, category, channel, assignee and customer name, then every thread and comment with who wrote it and when, oldest first. Threads are labelled `Customer (name)`, `Agent (name)` or `Internal thread (name)`, comments `Internal comment (name)` or `Comment (name)`. One per published article: title, department, category and body. Rendering does not depend on the order Zoho sends things in, so an unchanged ticket keeps its content hash. Each row carries its department's name as its node set, so recall can be scoped to one department. |
| First sync | Every ticket is listed (50 a request) and read with its conversation. A thread's full text is read from `/tickets/{id}/threads/{thread}`; the conversation listing only has a summary. |
| Later syncs | The Search API returns the tickets modified since the cursor (`modifiedTimeRange`). A new reply or comment moves a ticket's `modifiedTime`, so a changed ticket is read again in full. The search index can lag behind a change a little, so each window starts 10 minutes before the cursor; tickets whose `modifiedTime` was already synced are not read again. Articles are listed in full (see Deletes), and only those whose `modifiedTime` changed are read again. The cursor is Zoho's clock (`Date` header), not this machine's. |
| Deletes | A deleted ticket goes to Zoho Desk's Recycle Bin for 60 days; each run reads it first and forgets the stored tickets it lists (the `_deleted` hard-delete marker, which dlt drops on `merge` and cognee's orphan cleanup removes from the graph, vector and relational stores). Right after a delete the Search API can still return the ticket, and reading its conversation answers 403, so tickets in the bin are not read. A deleted article goes to the Knowledge Base's own trash instead (checked on a Zoho Desk account): it drops out of the article listing, and a lookup by id still answers with `isTrashed: true`. So the article listing is read in full every run (50 a request), and a stored article missing from it is looked up and forgotten when it is trashed, unpublished or gone. A ticket or article that moves out of the selected departments is forgotten too. |
| Long gaps | A sync more than 50 days after the last full read lists every ticket again, so a delete the Recycle Bin no longer holds is still caught. A stored ticket missing from that listing (an archived one, say) is looked up on its own and only forgotten if it is really gone. |
| Credits | Zoho Desk meters API calls in credits per day. Every answer reports what is left (`X-Rate-Limit-Remaining-v3`); below 50 the run stops cleanly (`failed_rate_limit`), what was read is kept, the cursor stays, and the next run resumes. |
| Errors | A refused token, a redirect or a 200 without JSON stop the run, and dlt keeps the previous state. An access token is refreshed when it expires or a call answers 401. 429, 5xx and dropped connections are retried, honouring `Retry-After` up to 30 s; a longer wait stops the run cleanly. An empty listing (Zoho answers 204) is an empty list. |

## Resetting a sync

The cursors and fingerprints live in dlt's pipeline state (under `~/.dlt/pipelines`, or
`$DLT_DATA_DIR/pipelines`), not in cognee's data folders. Pruning cognee does not reset them,
so the next sync would only fetch changes. To re-sync from scratch, delete that pipeline
folder as well. The state is kept per `resource_name` and dataset (`PIPELINE_SCOPE_ATTR`).

## Privacy

Support tickets hold customers' personal data. What the connector does:

**What is read**

- Tickets of the selected departments: subject, description, status, priority, category,
  channel, assignee and customer names, threads and (unless `include_comments=False`)
  comments. Published articles: title, category and body.
- People are named by name only. Email addresses and phone numbers are never written into a
  document, even though the API returns them.
- Never put in a document: attachments, custom fields, draft or unpublished articles.
- Internal threads and comments are labelled as internal, so an answer can tell what the
  customer was told from what the team said among themselves. Turn comments off with
  `include_comments=False` if they should not reach memory at all.
- The connector is read-only (GET requests, plus the token refresh). It never changes
  anything in Zoho Desk.

**Where it goes**

- The text above becomes cognee documents. During cognify it is sent to your configured LLM
  and embedding providers, like any document you add to cognee. Use local models if that
  matters to you.
- Anyone who can read the cognee dataset can recall what the synced tickets say. Share it
  only with people who may read those departments in Zoho Desk.
- dlt state stores the organization id, the cursors, and per ticket and article its id,
  `modifiedTime` and a short fingerprint. It does not store ticket text.
- Logs and error messages carry counts, HTTP statuses and endpoints with ids replaced, never
  ticket text or names.
- The client id, secret and refresh token are arguments, only sent to `accounts.zoho.<dc>`,
  and never logged or shown in a repr; the access token only goes to `desk.zoho.<dc>`.
  Redirects are not followed.

**How deletion is honoured**

- Deleted tickets (through the Recycle Bin) and deleted articles (through the Knowledge
  Base's trash) are forgotten on the **next sync**; so are unpublished articles, and
  tickets and articles moved out of the selected departments.
- `cognee.forget(dataset=...)` alone is not enough: dlt keeps its staging copy of the rows,
  so the next sync brings forgotten tickets back. Delete the ticket in Zoho Desk, or narrow
  `departments`, and the next sync forgets it.

## Limitations

- A ticket restored from the Recycle Bin comes back once it changes again, or at the next
  full read (after 50 days).
- A ticket deleted and then removed from the Recycle Bin before the next sync is only
  forgotten at the next full read (after 50 days, or when `departments` or
  `include_comments` change).
- Each run reads the whole Recycle Bin (one request per 50 items).
- Any change re-reads the whole ticket (one request per thread), so a long ticket costs a few
  credits each time it changes.
- A search that finds more than 2,000 changed tickets is replaced by a full read.
- The article listing is read in full every run (one request per 50 articles), because a
  deleted article only shows as missing from it.
- Articles without a department are kept whatever `departments` says.
- Only text is ingested: no attachments, custom fields or images.

## Testing

```bash
uv run pytest tests/
```

No Zoho account is needed. `tests/fake_zoho_desk.py` is an in-memory Zoho Desk API with the
behaviour seen on a real account.

- `test_zoho_desk.py` covers rendering (customer, agent, internal thread and comment labels;
  names, never addresses or phone numbers; time order; HTML), the first full read and the
  Search API cursor, replies and comments added and edited, repeats in the overlap window, a
  search index that lags, the Recycle Bin (a just-deleted ticket the search still returns,
  a ticket deleted mid-run), trashed and purged articles, departments (selected, changed,
  moved out), articles published, edited, unpublished and deleted, `include_comments` and
  `include_articles`, the full read after a long gap, archived tickets missing from the
  listing, listings that count from 1, the credit budget, rate limits, organizations, the
  HTTP client (token refresh, a 401 mid-run, 204, redirects, retries, the credentials), a
  sync through the real client, config validation, `check_active`, the sync stats, and real
  dlt merges into SQLite (including a failed load and two organizations).
- `test_zoho_desk_forget.py` runs the real cognee pipeline (LLM and embeddings mocked). It
  checks that a deleted ticket, a deleted article and a dropped department's ticket lose
  their Data records and leave the graph while an untouched ticket keeps its id, and that a
  ticket's node set names its department.
