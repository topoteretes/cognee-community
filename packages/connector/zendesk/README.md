# cognee-community-connector-zendesk

A Zendesk data-source connector for [cognee](https://github.com/topoteretes/cognee): sync
support tickets (with their comment threads) and Help Center articles into memory, so you
can ask "what did customers report about SSO this week, and how did we fix it?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each ticket and each
published article becomes one document and goes through cognee's normal cognify
pipeline, via the document-mode marker, the same way the Notion connector works.

## Requirements

cognee **1.6.3** (pinned). cognee 1.4 skips orphan cleanup when the last row of a table is
deleted, so the final deleted record was never forgotten. This was verified end to end:
on 1.4 the last deleted record stayed in memory, on 1.6.3 it is removed and comes back if
restored.

## Install

```bash
uv pip install cognee-community-connector-zendesk
# or, from this monorepo:
cd packages/connector/zendesk && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_zendesk import zendesk_source

await cognee.remember(
    zendesk_source(subdomain="acme"),  # credentials from the environment
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

Options:

| Option | Default | Meaning |
|---|---|---|
| `resources` | `("tickets", "articles")` | What to ingest. |
| `include_internal_notes` | `False` | Also ingest private agent notes (labelled `[internal note]`). |
| `locale` | account default | Help Center locale to list articles for. |
| `max_export_pages_per_run` | `None` | Cap ticket export pages (up to 1000 tickets each) for a large first backfill; the next run continues from the saved cursor. |

## Setup and auth

Set `ZENDESK_SUBDOMAIN` (the `acme` in `acme.zendesk.com`), then **one** of:

- **OAuth access token** (`ZENDESK_OAUTH_TOKEN`, sent as `Bearer`). Use this for new
  accounts. Zendesk is retiring API tokens: accounts created after 2026-07-28 cannot
  create them, creation stops for everyone on 2026-10-27, and existing tokens stop
  working on 2027-04-30 ([migration guide](https://developer.zendesk.com/documentation/authentication/oauth-migration/)).
  Create an OAuth client in Admin Center → Apps and integrations → APIs → OAuth clients,
  run the authorization-code flow with the `read` scope, and pass the access token. The
  connector does not run the OAuth flow or refresh tokens itself. Access tokens can be
  short-lived (on a trial account they lasted about 30 minutes), and the token has
  to stay valid for the whole run, so split a very large first backfill with
  `max_export_pages_per_run`.
- **Email + API token** (`ZENDESK_EMAIL` + `ZENDESK_API_TOKEN`) for existing setups.

The ticket export needs an admin's credentials. Articles need Help Center (Guide) to be
enabled.

## How sync works

| Concern | Behaviour |
|---|---|
| Tickets, incremental | Cursor-based incremental ticket export (`/api/v2/incremental/tickets/cursor.json`), not search, which is capped and drops older tickets. The first run starts at `start_time=0`; the `after_cursor` is kept in dlt resource state and only saved after a successful load. |
| Comments | A ticket comes back in the export when only a comment is added (tested on a trial account), so each changed ticket is re-rendered with its full thread (`/tickets/{id}/comments`, authors sideloaded). |
| Deleted tickets | They come through the export with `status="deleted"` and are emitted with the `_deleted` hard-delete marker, so cognee's `orphan_cleanup` forgets them. |
| Several changes in one window | The export lists a ticket once per change, so one run can see it several times (for example deleted and then restored). Only the newest version is kept, so the last change wins and comments are fetched once. |
| Articles | The Help Center has no delete feed, so articles are listed in full each run. That listing already carries `updated_at`, so it also finds edits, and the incremental articles endpoint would only add requests. Only published articles whose `updated_at` changed are re-emitted; drafts are skipped. |
| Removed articles | An article missing from a complete listing (archived or deleted), or turned back into a draft, is emitted as a tombstone. A failed listing raises instead, so it can never forget live articles. |
| Stable documents | A ticket document leaves out `updated_at`, which Zendesk bumps for metadata-only changes, so unchanged conversations keep their content hash and are not re-cognified. |
| Rate limits | 429 and 5xx responses are retried, honouring `Retry-After`. The export allows 10 requests a minute. |

## Resetting a sync

The export cursor and the article versions live in dlt's pipeline state (under
`~/.dlt/pipelines`, or `$DLT_DATA_DIR/pipelines`), not in cognee's data folders. Pruning
cognee does not reset them, so the next sync would only fetch changes. To re-sync from
scratch, delete that pipeline folder as well. On cognee 1.6+ this state is kept per
Zendesk subdomain and dataset (`PIPELINE_SCOPE_ATTR`).

## Privacy and security

- Credentials come from the environment or arguments, are only sent to
  `https://<subdomain>.zendesk.com`, and are never logged. Pagination links to any other
  host are refused.
- The connector is read-only (GET requests only).
- Tickets contain customer personal data. Internal notes are excluded by default. Keep the
  data in its own dataset so you can remove it with one `cognee.forget(dataset=...)` call.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Zendesk API with responses shaped like a real trial account's. They
cover rendering, the export cursor and resume, comment-only changes, deleted tickets, a
ticket changed several times in one window, article edit / archive / unpublish / no-op,
the failed-listing guard, the export page cap, auth and config errors, the dlt wiring,
the row-to-document mapping in cognee, and an end-to-end dlt merge into SQLite.
