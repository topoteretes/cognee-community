# cognee-community-connector-zendesk

A Zendesk data-source connector for [cognee](https://github.com/topoteretes/cognee): sync
support tickets (with their comment threads) and Help Center articles into memory, so you
can ask "what did customers report about SSO this week, and how did we fix it?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each ticket and each
published article becomes one document and goes through cognee's normal cognify
pipeline, via the document-mode marker, the same way the Notion connector works.

## Requirements

- cognee **1.6.3** or later (below 2). cognee 1.4 skips orphan cleanup when the last row of
  a table is deleted, so the final deleted record was never forgotten. This was verified end
  to end: on 1.4 the last deleted record stayed in memory, on 1.6.3 it is removed and comes
  back if restored.
- Python 3.10 to 3.13.
- A Zendesk account with admin access (the ticket export needs it), and Help Center
  (Guide) enabled for articles.

## Install

```bash
uv pip install cognee-community-connector-zendesk
# or, from this monorepo:
cd packages/connector/zendesk && uv sync
```

## Usage

```python
import os

import cognee
from cognee_community_connector_zendesk import zendesk_source

await cognee.remember(
    zendesk_source(subdomain="acme", oauth_token=os.environ["ZENDESK_OAUTH_TOKEN"]),
    dataset_name="zendesk",
    primary_key="id",
    write_disposition="merge",  # REQUIRED
    max_rows_per_table=0,
    self_improvement=False,
)

answers = await cognee.recall(
    "What did customers report about SSO, and how was it fixed?", datasets=["zendesk"]
)
```

`write_disposition="merge"` is required: cognee applies one write disposition to the whole
source, and the incremental export only returns what changed, so the default (`replace`)
would wipe earlier tickets on the second sync.

Settings and credentials are arguments only; `examples/example.py` reads them from
`ZENDESK_SUBDOMAIN`, `ZENDESK_OAUTH_TOKEN` (or `ZENDESK_EMAIL` + `ZENDESK_API_TOKEN`).

| Option | Default | Meaning |
|---|---|---|
| `subdomain` | required | The `acme` of `acme.zendesk.com`. Validated, so credentials only ever go to that host. |
| `oauth_token`, or `email` + `api_token` | required | See [Setup and auth](#setup-and-auth). |
| `resources` | `("tickets", "articles")` | What to ingest. |
| `include_internal_notes` | `False` | Also ingest private agent notes (labelled `[internal note]`). Changing it makes the next run read the whole export, so turning it off removes the notes already stored in one sync. |
| `include_emails` | `False` | Name people as `Name <email>`. By default documents carry names only. |
| `locale` | all | Help Center locale to list articles for. |
| `max_export_pages_per_run` | `1` | Ticket export pages read per run (up to 1000 tickets each). The next run continues from the saved cursor, so a large first backfill makes progress on every run; `None` reads the whole export in one run. |
| `resource_name` | `zendesk_<subdomain>` | Prefix of the table names (`<resource_name>_tickets`, `<resource_name>_articles`) and key of the sync state: lowercase letters, digits and underscores. A state written for another account is refused. Keep it fixed across runs. |
| `check_active` | `None` | Optional callable checked before every API request, before every retry wait and around every row; raising from it stops the sync (for hosts that need to stop a sync whose access was revoked). |

## Setup and auth

Pass `subdomain` (the `acme` in `acme.zendesk.com`), then **one** of:

- **OAuth access token** (`oauth_token=`, sent as `Bearer`). Use this for new
  accounts. Zendesk is retiring API tokens: accounts created after 2026-07-28 cannot
  create them, creation stops for everyone on 2026-10-27, and existing tokens stop
  working on 2027-04-30 ([migration guide](https://developer.zendesk.com/documentation/authentication/oauth-migration/)).
  Create an OAuth client in Admin Center → Apps and integrations → APIs → OAuth clients,
  run the authorization-code flow with the `read` scope, and pass the access token. The
  connector does not run the OAuth flow or refresh tokens itself. Access tokens can be
  short-lived (on a trial account they lasted about 30 minutes), and the token has
  to stay valid for the whole run. By default each run reads one export page (up to 1000
  tickets), so a large first backfill is spread over several runs and saves its progress
  each time.
- **Email + API token** (`email=` + `api_token=`) for existing setups.

The ticket export needs an admin's credentials. Articles need Help Center (Guide) to be
enabled.

## How sync works

| Concern | Behaviour |
|---|---|
| Tickets, incremental | Cursor-based incremental ticket export (`/api/v2/incremental/tickets/cursor.json`), not search, which is capped and drops older tickets. The first run starts at `start_time=0`; the `after_cursor` is kept in dlt resource state and only saved after a successful load. |
| Comments | A ticket comes back in the export when only a comment is added (tested on a trial account), so each changed ticket is re-rendered with its full thread (`/tickets/{id}/comments`, authors sideloaded; anyone else is looked up with `users/show_many`, and a failed lookup falls back to `user <id>`). |
| One ticket that fails | Its comments answering 404 or 410 means it was deleted after the export listed it, so it is forgotten. A 403, a 422 or a 5xx that outlasts the retries sets it aside: the cursor moves on, the rest of the page syncs, and the ticket is asked for again on the next runs, then given up after three (it stays as stored). Three different tickets failing with a fresh 5xx in a row mean Zendesk itself is likely down: the run stops cleanly, keeping what it read and setting the rest of the page aside, and the next run asks for those as retries (which never count toward that streak, so tickets that stay broken cannot stop every run). A record without an id is skipped. |
| Deleted tickets | They come through the export with `status="deleted"` and are emitted with the `_deleted` hard-delete marker, so cognee's `orphan_cleanup` forgets them. |
| Several changes in one window | The export lists a ticket once per change, so one run can see it several times (for example deleted and then restored). Only the newest version is kept, so the last change wins and comments are fetched once. |
| Articles | The Help Center has no delete feed, so articles are listed in full each run, with cursor pagination (offset pagination stops at 10,000 records). That listing already carries `updated_at`, so it also finds edits. Only published articles whose `updated_at` changed are re-emitted; drafts are skipped. Articles are keyed by id and locale, so two translations of one article are two documents. |
| Removed articles | An article missing from a complete listing (archived or deleted), or turned back into a draft, is emitted as a tombstone. Cursor pages carry no total, so the `count` comes from one offset request first; a failed listing, or one that does not add up to that count, raises instead, so it can never forget live articles. |
| Pagination that does not move | An export cursor, a comment page or an article page that hands back the same position stops instead of looping. |
| Stable documents | A ticket document leaves out `updated_at`, which Zendesk bumps for metadata-only changes, so unchanged conversations keep their content hash and are not re-cognified. |
| Large accounts | With one export page per run, an account with more than about 1000 ticket changes between syncs falls behind; when a run stops at the cap it logs a warning with how many hours behind the export is. |
| Rate limits | 429, 5xx and dropped connections are retried, honouring `Retry-After` up to 30 s. A longer wait, or a rate limit that outlasts the retries, stops the rest of the run cleanly (no rows, no state change, `failed_rate_limit`), and the next run resumes. cognee holds a process-wide lock while a dlt source runs, so the connector does not sleep through long waits. The export allows 10 requests a minute. |
| Failures | Tickets and articles fail separately: when one gets an error from Zendesk (Help Center switched off, say), the other still syncs and saves its progress, and the failed one keeps its old state and is retried on the next run. Raising there would fail the whole run, and dlt would keep neither part's progress, so a part that fails three runs in a row raises with the reason instead. Bad credentials (401), a ticket export the token may not read (403), no connection, or both failing end the run at once, and dlt keeps the previous state. A 200 without a JSON body (a login or maintenance page) ends the run, also on one ticket's request, instead of setting records aside. |
| Sync stats | After a run, `source.cognee_sync_stats` holds counts only: `scanned`, `deleted`, `kept` (tickets set aside), `given_up`, `failed`, plus `failed_tickets` / `failed_articles` / `failed_auth` / `failed_rate_limit` / `failed_outage`. |

## Resetting a sync

The export cursor and the article versions live in dlt's pipeline state (under
`~/.dlt/pipelines`, or `$DLT_DATA_DIR/pipelines`), not in cognee's data folders. Pruning
cognee does not reset them, so the next sync would only fetch changes. To re-sync from
scratch, delete that pipeline folder as well. The state is kept per `resource_name` and
dataset (`PIPELINE_SCOPE_ATTR`).

## Privacy

Tickets hold customer personal data. Be clear about what the connector does:

**What is read**

- Tickets: subject, status, priority, type, tags, created date, the requester's and
  assignee's name, and each comment's author, date and plain text. Email addresses only
  with `include_emails=True`.
- Internal notes only when `include_internal_notes=True` (they are labelled
  `[internal note]`); they are left out by default.
- Help Center articles: published ones only, with their title, body (as text), labels,
  locale and link. Drafts are skipped.
- Never put in a document: attachments, custom ticket fields, satisfaction ratings and
  organizations (the export returns some of these, the connector drops them), and
  nothing is requested outside the selected `resources`.
- The connector is read-only (GET requests only). It never creates, changes or deletes
  anything in Zendesk.

**Where it goes**

- The text above becomes cognee documents. During cognify it is sent to your configured LLM
  and embedding providers, like any document you add to cognee. Use local models if that
  matters to you.
- Anyone who can read the cognee dataset can recall what the synced tickets say, including
  customer names (and emails and internal notes when they are on). Share the dataset only
  with people who may read those tickets in Zendesk.
- dlt state stores the export cursor, the subdomain, the `include_internal_notes`
  setting, the ids of tickets set aside, and each article's id, locale and `updated_at`.
  It does not store ticket or article text.
- Logs and error messages carry counts, HTTP statuses and endpoints, never ticket ids,
  names or ticket content.
- Credentials are arguments, are only sent to `https://<subdomain>.zendesk.com`, and are
  never logged or shown in a repr. Surrounding whitespace (a pasted newline) is stripped,
  and a credential with whitespace inside is refused without echoing it. The subdomain is validated, pagination links to any
  other host or over plain http are refused, and redirects are not followed.

**How deletion is honoured**

- Deleted tickets, and archived, deleted or unpublished articles, are forgotten on the
  **next sync**.
- Turning `include_internal_notes` off removes the notes already stored on the next sync.
- `cognee.forget(dataset=...)` alone is not enough: dlt keeps its staging copy of the rows,
  so the next sync brings forgotten tickets back, even with the pipeline folder deleted.
  Delete the tickets (or archive the articles) in Zendesk, and the next sync forgets them.

## Limitations

- The ticket export needs an admin's credentials and allows 10 requests a minute. Each
  changed ticket also costs one comments request, so a large first backfill takes several
  runs (one export page each by default).
- Articles have no delete feed, so every run lists all of them (one request per 100
  articles).
- The connector does not run the OAuth flow or refresh OAuth tokens; the token has to stay
  valid for the whole run.
- Ticket and article ids are only unique inside one Zendesk account. Each account gets its
  own tables through `resource_name` (by default from the subdomain), so two accounts can
  share a dataset; do not reuse one `resource_name` for two accounts.
- Taking `tickets` or `articles` out of `resources` stops syncing them but does not forget
  what was already stored; forget the dataset to drop it.
- Attachments are not ingested.

## Testing

```bash
uv run pytest tests/
```

No Zendesk account or API key is needed. `tests/fake_zendesk.py` is an in-memory Zendesk
with responses shaped like a real trial account's.

- `test_zendesk.py` covers rendering (names only by default, emails opt-in), the export
  cursor and resume, comment-only changes, deleted tickets, a ticket changed several times
  in one window, turning internal notes off (the whole export at once), one ticket whose
  comments fail (404/410 forgotten, 403/422/5xx set aside, retried and given up, three
  5xx in a row stopping the run), a failing user lookup, an export the token may not read,
  article edit / archive / unpublish / publish again / no-op, cursor pagination, two
  translations, pagination that does not move, the count guard, rendering independent of
  input order, the HTTP client (retries, dropped connections, a long `Retry-After`,
  redirects, plain http, other hosts, a 200 without JSON, record ids kept out of errors,
  a redacted repr), a sync through the real client, config validation (subdomain,
  `resource_name`, locale, arguments only), a state from another account, per-account
  tables, `check_active`, tickets and articles failing separately, a part that keeps
  failing raising on its third run, a rate limit stopping the run cleanly, a large
  backfill saving progress every run, the lag warning, the sync stats, the row-to-document
  mapping in cognee, and an end-to-end dlt merge into SQLite.
- `test_zendesk_forget.py` runs the real cognee pipeline (LLM and embeddings mocked, like
  the google-drive connector's forget test). It checks that a deleted ticket and an
  archived article lose their Data records and leave the graph while an untouched ticket
  keeps its id, and that two accounts with the same ticket id in one dataset stay apart.
