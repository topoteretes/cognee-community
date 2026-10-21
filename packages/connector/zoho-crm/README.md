# cognee-community-connector-zoho-crm

A Zoho CRM data-source connector for [cognee](https://github.com/topoteretes/cognee): turn your
CRM into memory - "which deals with Acme are stuck, and what did we promise them?".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Records (Leads,
Contacts, Accounts, Deals or any module), their notes and, optionally, text attachments are
ingested as **normal documents** (cognee document-mode), so they go through cognify entity extraction.

## Requirements

* cognee **>= 1.6.1, < 2**. The connector uses `PIPELINE_SCOPE_ATTR`, `guarded_rows` and table-scoped
  document cleanup (`DOCUMENT_SYNC_VERSION >= 1`) and refuses to run on an older core, so a sync can
  never delete another source's data.
* Python 3.10 - 3.13.

## Install

```bash
uv pip install cognee-community-connector-zoho-crm
# or, from this monorepo:
cd packages/connector/zoho-crm && uv sync --all-extras
```

## Setup (OAuth 2.0, Self Client)

Zoho runs separate data centres. **Use the API console of your own region** - for example
`api-console.zoho.eu` for EU or `api-console.zoho.in` for India. Using the wrong one fails with
`invalid_client`.

1. Open `https://api-console.zoho.<region>` -> **Add Client** -> **Self Client**. Copy the
   **Client ID** and **Client Secret**.
2. **Generate Code** tab. Read-only scope: `ZohoCRM.modules.READ,ZohoCRM.settings.READ`.
   (On a live account this scope also lists and downloads record attachments. If your org splits
   scopes, add `ZohoCRM.modules.attachments.READ`.) Create the code and copy it.
3. Exchange the code for a refresh token (once, within the code's lifetime):

   ```bash
   curl -X POST "https://accounts.zoho.<region>/oauth/v2/token" \
     -d grant_type=authorization_code -d client_id=$ZOHO_CLIENT_ID \
     -d client_secret=$ZOHO_CLIENT_SECRET -d code=<the code>
   ```

   Keep the `refresh_token` from the answer. It does not expire unless revoked.
4. The connector only takes credentials as arguments; the example reads them from these variables:

```bash
export ZOHO_CLIENT_ID="1000...."
export ZOHO_CLIENT_SECRET="..."
export ZOHO_REFRESH_TOKEN="1000...."
export ZOHO_REGION="eu"     # com, eu, in, com.au, jp, com.cn, ca, sa
export LLM_API_KEY="sk-..."
```

## Usage

```python
import os

import cognee
from cognee_community_connector_zoho_crm import zoho_crm_source

source = zoho_crm_source(
    resource_name="zoho_acme",  # one per Zoho org, keep it fixed
    client_id=os.environ["ZOHO_CLIENT_ID"],
    client_secret=os.environ["ZOHO_CLIENT_SECRET"],
    refresh_token=os.environ["ZOHO_REFRESH_TOKEN"],
    region="eu",
    modules=["Leads", "Deals"],
)
await cognee.remember(
    source,
    dataset_name="zoho_crm",
    primary_key="id",
    write_disposition="merge",  # REQUIRED, see below
)
print(source.cognee_sync_stats)  # integer counts for the run
```

All arguments are keyword-only.

| Argument | Meaning |
| --- | --- |
| `resource_name` | Stable connection name (lowercase, digits, `_`). Prefixes the tables (`<name>_records`, `<name>_notes`, `<name>_attachments`) and scopes the sync state. Default `zoho_crm`. |
| `client_id`, `client_secret`, `refresh_token` | OAuth credentials (required). |
| `region` | Data centre: `com` (default), `eu`, `in`, `com.au`, `jp`, `com.cn`, `ca`, `sa`. |
| `modules` | Module API names. Default: `Leads`, `Contacts`, `Accounts`, `Deals`. Changes are handled (see below). |
| `include_notes` | Sync notes on records of those modules (default `True`). |
| `include_attachments` | Sync text-like attachments: `.txt`, `.md`, `.csv`, `.json`, `.html`, ... (default `False`). |
| `attachment_max_bytes` | Skip larger attachments (default 1 MB). |
| `redact_contact_details` | Leave out e-mail and phone fields (default `True`). |
| `reconcile` | Find records that vanished without a delete event (default `True`). |
| `reconcile_every` | Reconcile on every N-th run (default `1`). |
| `modified_since` | ISO 8601 time that limits the **first** run only. |
| `max_rate_limit_wait` | Longest `Retry-After` in seconds that is slept (default 120); a longer one stops the run cleanly. |
| `check_active` | Optional host authorization callback, called before each request and around each row. |
| `service` / `client` | Pre-built `httpx.Client`-like object (mainly for tests). |

> **`write_disposition="merge"` is required.** cognee defaults to `replace`, which reloads the
> table each run. An incremental run only sees what changed, so `replace` would forget the rest.

`source.cognee_sync_stats` holds integer counts: `records_synced`, `records_forgotten`, `notes_synced`,
`notes_forgotten`, `attachments_synced`, `attachments_skipped`, `attachments_forgotten`,
`reconcile_skipped`, `failed_rate_limit`, `failed_auth`.

## What a document contains

* **Record** (`id = <Module>:<id>`): the module and each useful field as `Label: value`, using the
  labels from your CRM's field settings. Lookups are shown by name; multi-select values are sorted.
  `url` links back to the record (`https://crm.zoho.<dc>/crm/tab/<Module>/<id>`).
* **Note** (`id = Notes:<id>`): title, "Note on Deal: <name>", author, created time and the text;
  `url` links to the parent record.
* **Attachment** (`id = Attachments:<id>`): "Attachment on <record>: <file name>" and the file text.

**Fields are chosen by type**, from each module's field metadata, so custom fields follow the same
rules. Left out:

* date-times (except `Created_Time`), formula, auto-number, roll-up, image and file fields, and
  volatile fields such as visit tracking (`Last_Visited_Time`, `Days_Visited`, `Number_Of_Chats`,
  `Visitor_Score`, ...), modified-by and durations. A page view or a recalculation therefore never
  changes a record's text, and the record is not re-extracted;
* e-mail and phone fields, unless `redact_contact_details=False`.

## How it works

**Fields.** Zoho requires a `fields` list on every list call and rejects more than 50 (the standard
Leads module has 55). The connector asks for at most 50 (see Known gaps).

**Incremental sync.** Every list call sends `If-Modified-Since: <cursor>`; Zoho answers `304 Not
Modified` when nothing changed. The filter is inclusive, so a record changed at the cursor time is
read again; `merge` makes that harmless. Cursors come from **Zoho's clock** (the `Date` header of the
run's first response) minus a 5-minute overlap, so a local clock that runs ahead cannot hide changes.
The record-module cursors move together only after every module finishes. Records, notes and
attachments are separate dlt resources: a resource stopped by a long rate limit moves no cursor,
while a resource that already finished keeps its cursor.

**Paging.** Record lists follow `next_page_token`, so there is no 2,000-record ceiling. `GET /Notes` is
documented with `page` / `per_page` only (a live v8 account also returns a token); the connector follows
the token when there is one and falls back to page numbers otherwise. A repeated token, or a record
returned twice, raises instead of ending the listing quietly.

**Forget-on-delete.**

* The Deleted Records API (`/<module>/deleted?type=all`, sent with `If-Modified-Since`, so a quiet run
  gets `304`s) turns deleted records and notes into `{"id": ..., "_deleted": True}` tombstones. Notes
  and attachments of a deleted record are tombstoned too.
* Every `reconcile_every` runs, each module's ids are listed and every id that vanished is re-checked
  with a single `GET` before it is forgotten (`204` = gone; a lead with `Converted__s` = converted).
  This catches **converted leads**, which leave `Leads` without appearing as deleted, and records whose
  delete event is older than the deleted feed keeps. At most 200 ids are confirmed per module and run;
  the rest wait for the next run, which reconciles at once (not after `reconcile_every`). Ids
  confirmed alive stay out of the batch for the rest of that cycle, so live records that the id
  listing never shows cannot make every run pay for a full check. When nothing is left, the
  `reconcile_every` schedule resumes and the next reconcile checks all ids again. A listing that
  repeats an id or page token forgets nothing that run (`reconcile_skipped`).
* A module removed from `modules` is forgotten, with its notes and attachments. A module added later
  is backfilled, including its older notes and attachments.

dlt removes tombstoned rows on `merge`, and cognee's orphan cleanup then deletes those documents
(see `tests/test_cognee_add.py`).

**Attachments** are listed per record (`GET /<Module>/<id>/Attachments`, as the v8 docs describe):
for every record changed since the last run (all records on the first run). Attachments no longer on
a re-listed record are forgotten.

**Failures.** `5xx` and network errors are retried with backoff. A `Retry-After` up to
`max_rate_limit_wait` is slept; a longer one stops all interleaved resources before their next API
request. The stopped resource moves no cursor; already completed resources keep theirs, and completed
attachment-parent checkpoints and reconcile results (forgotten and confirmed-alive ids) are saved,
so a large first run resumes and a stopped reconcile does not repeat its checks. A
checkpoint includes the parent's `Modified_Time`, so a parent changed while waiting is processed again.
Any other error aborts the run. Errors carry the HTTP status and Zoho's error code, never response bodies.

## Known gaps

* **Deleted-records retention.** Zoho keeps deleted records for about 60 days (recycle bin) and 120
  days (permanently deleted). If the connector does not run within that window, the delete event is
  gone; the reconcile still finds the record missing and forgets it.
* **Converted leads** leave `Leads` without a delete event; only the reconcile notices them. With a
  large `reconcile_every`, they stay in memory until the next reconcile.
* **`GET /Notes` needs an admin profile.** With a non-admin token, set `include_notes=False`.
* **More than 49 usable fields.** A module whose usable fields still exceed 49 is cut to the first 49
  (Zoho's limit is 50 including `id`); a warning is logged.
* **Attachment changes without editing the record.** Attachments are re-listed for records that
  changed. If adding or removing a file does not bump the record's modified time in your org, that
  change is noticed only the next time the parent record changes.
* **State lives in dlt.** Cursors and known ids are in the dlt pipeline state; if it is lost, the next
  run is a full backfill.
* **Credits.** Zoho meters API use in credits per day. A quiet run costs about two requests per module
  (`304` list + `304` deleted feed) plus field metadata. A reconcile adds one request per 200 records.
  Attachment sync fans out to one list request per changed parent record, plus one download per kept file.
  Reconciliation also uses one confirming GET per candidate that vanished from the id listing (up to 200
  per run); ids confirmed alive are remembered until the backlog cycle ends, so repeatedly-live ids
  cannot starve later candidates. If the listing hides many live records, a cycle needs about
  `hidden / 200` runs, and with `reconcile_every=1` each new cycle starts at the next run.

## Privacy

CRM data is customer data. Nothing is fetched until you run the connector. The scope above is
read-only, contact details are left out by default, and you can limit `modules`. Use a dedicated
dataset so you can remove it with one `cognee.prune`.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake Zoho CRM v8 API that follows the docs and the live behaviour above (mandatory
`fields` capped at 50, `next_page_token` for records, **page numbers for Notes**, inclusive
`If-Modified-Since` with `304`, deleted feed with `If-Modified-Since`, per-record attachments, `204`
for a deleted record, a `Date` header, `invalid_client` for a wrong region). They cover field
selection by type, visit-tracking fields, sorted multi-selects, links, OAuth and region errors, error
messages without bodies, rate limits (short ones retried, long ones stop cleanly), 450 notes paged by
number, repeated tokens, cursors that only move together (including on a clean stop), deletes,
converted leads, the reconcile re-check, `reconcile_every`, adding and removing modules (with their
notes), attachments, two sources in one dataset, and a run through `cognee.add`. The `cognify` graph
check in `tests/test_cognee_add.py` runs when `LLM_API_KEY` is set.
