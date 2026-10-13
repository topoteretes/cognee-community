# cognee-community-connector-hubspot

A HubSpot data-source connector for [cognee](https://github.com/topoteretes/cognee): sync
contacts, companies, deals, tickets and their notes, calls, meetings, emails and tasks into
memory, with the relationships between them, so you can ask "which deals is Jane Doe on,
and what did we last discuss with Acme?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each record becomes one
document that names the records it is linked to, and goes through cognee's normal cognify
pipeline via the document-mode marker, the same way the Notion connector works.

## Requirements

cognee 1.6.3 (pinned). Older 1.4 releases skip orphan cleanup once a table becomes empty,
so the last deleted record would stay in memory. 1.6.3 handles that case.

## Install

```bash
uv pip install cognee-community-connector-hubspot
# or, from this monorepo:
cd packages/connector/hubspot && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_hubspot import hubspot_source

await cognee.remember(
    hubspot_source(objects=["contacts", "companies", "deals", "tickets", "notes"]),
    dataset_name="hubspot",
    primary_key="id",
    write_disposition="merge",  # REQUIRED
    max_rows_per_table=0,
    self_improvement=False,
)

answers = await cognee.recall("Which deals is Jane Doe on?", datasets=["hubspot"])
```

`write_disposition="merge"` is required: the cursor only returns what changed, so the
pipeline default (`replace`) would wipe earlier records on the second sync.

Options:

| Option | Default | Meaning |
|---|---|---|
| `objects` | contacts, companies, deals, tickets, notes | Also available: calls, meetings, emails, tasks. |
| `extra_properties` | none | More properties to read per object, e.g. `{"contacts": ["phone"]}`. |
| `reconcile_deletes` | `False` | Also list every id, to catch records removed for good (one request per 100 records). |
| `portal_id` | looked up | The HubSpot account id, used for record links and to keep sync state per account. |

## Setup and auth

**OAuth app** (what the issue asks for):

1. In HubSpot, create an app (**Development → Projects**, or **Legacy apps**) with the
   redirect URL `http://localhost:3000` and these read scopes:

   ```
   oauth                       crm.objects.contacts.read   crm.objects.companies.read
   crm.objects.deals.read      crm.objects.tickets.read    crm.schemas.contacts.read
   crm.schemas.companies.read  crm.schemas.deals.read      crm.schemas.tickets.read
   sales-email-read
   ```

   Notes, calls, meetings and tasks are read with these too; email engagements need
   `sales-email-read`. Owner names also need `crm.objects.owners.read`; without it an
   owner shows as `owner <id>`. The old `tickets` scope is retired and fails when the
   app is deployed; use `crm.objects.tickets.read`.
2. Get a refresh token once. A browser opens, you approve the scopes, and the token is
   saved to a private file (`~/.cognee_hubspot_refresh_token`), never printed:

   ```bash
   uv run python examples/get_refresh_token.py
   ```

3. Set `HUBSPOT_CLIENT_ID`, `HUBSPOT_CLIENT_SECRET` and `HUBSPOT_REFRESH_TOKEN`. Each run
   mints a fresh access token (they last 30 minutes), and mints another if one expires
   during a long sync.

**Service Key or private app token** (`HUBSPOT_ACCESS_TOKEN`): new accounts can no
longer create legacy private apps; Service Keys replace them and work the same way.
Give it the same read scopes.

A record's linked records are only named when their object type is synced as well.

## How sync works

| Concern | Behaviour |
|---|---|
| Documents and relationships | One document per record. A contact lists its companies, deals and tickets by name; a company its contacts, deals and tickets; an engagement gets an `About:` line. cognify turns those names into entities, so the same company in several documents becomes one graph node. Deal and ticket stages and owners are shown by their labels. |
| Incremental cursor | Per object type, the search API on the last-modified property (`lastmodifieddate` for contacts, `hs_lastmodifieddate` for the rest), ascending. Search returns at most 10,000 results per query, so a bigger window restarts from the last timestamp seen. The saved cursor stays two minutes behind the run's start, because search indexing lags a little. It lives in dlt resource state, saved only after a successful load. |
| Relationships stay current | When a record's name or label changes, every document that names it is re-rendered too (notes included), so a renamed company is not left under its old name in its contacts' documents or in the notes about it. Adding or removing a link refreshes both sides. HubSpot also bumps the modified date on its own (activity counts, rollups); such a bump re-reads that record but does not ripple into its linked documents. |
| Forget-on-delete | Deleting a record archives it, and search never returns archived records, so the archived list (kept 90 days) is the delete feed: records archived since the last run become `_deleted` hard-delete rows and cognee's `orphan_cleanup` forgets them. Documents that named a deleted record are re-rendered without it. |
| Merged records | A merged-away record disappears without being archived. The survivor's `hs_merged_object_ids` names it, and it is tombstoned too. |
| Removed for good | GDPR deletes and records archived more than 90 days ago leave no trace in the archived list. `reconcile_deletes=True` compares a full id listing with what is stored; a failed listing raises instead of deleting. |
| Stable documents | Documents leave out modified timestamps, so an unchanged record keeps its content hash and is not re-cognified. |
| Rate limits | Requests go through dlt's REST client, which retries 429 and 5xx responses and honours `Retry-After`. Search calls are paced to its 5 requests per second. |

## Why not the dlt verified source as is

The issue asks to start from dlt's verified HubSpot source and to say where it and
cognee's needs disagree. Run on a free account:

- It is distributed through `dlt init hubspot` (copied into a project), not as a package
  a connector can depend on.
- It has no incremental cursor for CRM objects (the only cursor is for web analytics), so
  every run re-downloads everything.
- It does not read notes, calls, meetings, emails or tasks.
- Deletes do not propagate: a deleted contact stayed after a re-run.
- It reads every property by default (about 90 per contact, including date of birth and
  gender).

So the connector is built on the same dlt HTTP layer the verified source uses (dlt's
`RESTClient`, with its retry handling), and adds the cursor, engagements, deletions and a
property allowlist.

It also uses cognee's document path rather than the relational one. cognee's relational
dlt path only links rows through database foreign keys (read with `PRAGMA
foreign_key_list` in `ingest_dlt_source.py`), and dlt does not create those, so
association tables would land as unconnected rows and get no entity extraction. Writing
the linked records into each document by name gives cognify the relationships directly.

## Resetting a sync

The cursors, the stored ids, which records each document names, and a short hash of each
record's label (never the name itself) live in dlt's pipeline state (under
`~/.dlt/pipelines`, or `$DLT_DATA_DIR/pipelines`), not in cognee's data folders. Pruning cognee does not reset
them, so the next sync would only fetch changes. To re-sync from scratch, delete that
pipeline folder as well. On cognee 1.6+ this state is kept per HubSpot account and
dataset (`PIPELINE_SCOPE_ATTR`).

## Privacy and security

- Tokens come from the environment or arguments, are only sent to `api.hubapi.com`, and
  are never logged.
- The connector is read-only.
- CRM records describe named people. Only an allowlist of properties is read (no birth
  date, gender, phone or address by default); `extra_properties` adds more on purpose.
  Keep the data in its own dataset so you can remove it with one
  `cognee.forget(dataset=...)` call.

## Limitations

- The first run reads every record of the selected types.
- GDPR deletes and records archived more than 90 days ago are only caught with
  `reconcile_deletes=True` (one listing request per 100 records).
- Invoices, products, line items and custom objects are not synced yet. Invoices would be
  one more object type (with `crm.objects.invoices.read`) rendered the same way.
- Attachments, and email bodies beyond `hs_email_text`, are not ingested.

## Testing

```bash
uv run pytest tests/
```

No HubSpot account or API key is needed. `tests/fake_hubspot.py` is an in-memory HubSpot
that answers the endpoints the connector uses (search with the last-modified filter, the
archived listing, v4 associations, batch reads, owners and pipelines), shaped like a free
account's responses.

- `test_hubspot.py` covers rendering with relationships, stage and owner labels, the
  property allowlist, the cursor per object type, the cursor staying behind the search
  index, the 10,000-result window restart, related documents refreshed after a rename
  (notes included) or a delete, archived, merged and reconciled deletions, the
  failed-listing guard, missing scopes, auth and config errors, the dlt wiring, the
  row-to-document mapping in cognee, and an end-to-end dlt merge into SQLite with one
  table per object type.
- `test_hubspot_forget.py` runs the real cognee pipeline (LLM and embeddings mocked, like
  the google-drive connector's forget test) and checks that a deleted note and a deleted
  contact leave the graph, including the contact's name on her company's document, while
  an untouched note stays.
