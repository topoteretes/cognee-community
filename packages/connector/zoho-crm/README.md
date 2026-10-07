# cognee-community-connector-zoho-crm

A Zoho CRM data-source connector for [cognee](https://github.com/topoteretes/cognee): turn your
CRM into memory - "which deals with Acme are stuck, and what did we promise them?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Records (Leads, Contacts, Accounts,
Deals or any module), their notes and, optionally, text attachments are ingested as **normal
documents** (cognee document-mode), so they go through cognify entity extraction.

## Requirements

cognee **>= 1.4.0** (document-mode: `DOCUMENT_SOURCE_ATTR`).

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
2. **Generate Code** tab. Scope (read-only):
   `ZohoCRM.modules.READ,ZohoCRM.settings.READ`. Pick a duration, create the code and copy it.
3. Exchange the code for a refresh token (once, within the code's lifetime):

   ```bash
   curl -X POST "https://accounts.zoho.<region>/oauth/v2/token" \
     -d grant_type=authorization_code -d client_id=$ZOHO_CLIENT_ID \
     -d client_secret=$ZOHO_CLIENT_SECRET -d code=<the code>
   ```

   Keep the `refresh_token` from the answer. It does not expire unless revoked.
4. Export the values and your LLM key:

```bash
export ZOHO_CLIENT_ID="1000...."
export ZOHO_CLIENT_SECRET="..."
export ZOHO_REFRESH_TOKEN="1000...."
export ZOHO_REGION="eu"     # com (default), eu, in, com.au, jp, com.cn, ca, sa
export LLM_API_KEY="sk-..."
```

The connector exchanges the refresh token for a one-hour access token at
`accounts.zoho.<region>` and calls the API host Zoho returns (`api_domain`, e.g.
`https://www.zohoapis.eu`). It gets a new access token by itself when the old one expires.

## Usage

```python
import cognee
from cognee_community_connector_zoho_crm import zoho_crm_source

await cognee.remember(
    zoho_crm_source(region="eu", modules=["Leads", "Deals"]),
    dataset_name="zoho_crm",
    write_disposition="merge",  # REQUIRED, see below
)

answer = await cognee.search(
    query_text="Which deals are in negotiation and what are the open concerns?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["zoho_crm"],
)
```

| Argument | Meaning |
| --- | --- |
| `client_id`, `client_secret`, `refresh_token` | OAuth credentials. Default: `ZOHO_*` env vars. |
| `region` | Data centre: `com` (default), `eu`, `in`, `com.au`, `jp`, `com.cn`, `ca`, `sa`. Default: `ZOHO_REGION`. |
| `modules` | Module API names. Default: `Leads`, `Contacts`, `Accounts`, `Deals`. |
| `include_notes` | Sync notes on records of those modules (default `True`). |
| `include_attachments` | Sync text-like attachments: `.txt`, `.md`, `.csv`, `.json`, `.html`, ... (default `False`). |
| `attachment_max_bytes` | Skip larger attachments (default 1 MB). |
| `redact_contact_details` | Leave out e-mail and phone fields (default `True`). |
| `modified_since` | ISO 8601 time that limits the **first** run only. |

> **`write_disposition="merge"` is required.** cognee defaults to `replace`, which reloads the
> table each run. An incremental run only sees what changed, so `replace` would forget the rest.

See `examples/example.py` for a runnable script.

## What a document contains

* **Record** (`id = <Module>:<id>`), for example `Deals:4876876000000376008`: the module and every
  useful field as `Label: value`, using the labels from your CRM's own field settings. Lookups
  (owner, account, contact) are shown by name. Titles come from `Deal_Name`, `Account_Name`,
  `Full_Name`, and so on.
* **Note** (`id = Notes:<id>`): title, "Note on Deal: <name>", author, created time and the text.
* **Attachment** (`id = Attachments:<id>`): "Attachment on <record>: <file name>" and the file text.

Left out on purpose:

* **System and volatile fields** - `Modified_Time`, `Modified_By`, `Last_Activity_Time`, sales-cycle
  durations, enrichment status, images, coordinates. They change without the record's meaning
  changing, so leaving them out keeps the content hash stable: a record is only re-cognified when
  its real content changes.
* **E-mail and phone fields**, and owner e-mails, unless `redact_contact_details=False`.

## How it works

**Fields.** Zoho requires a `fields` list on every list call and rejects more than 50
(`LIMIT_EXCEEDED`; the standard Leads module has 55). The connector reads each module's field
metadata (`/settings/fields`), drops the fields above and asks for at most 50 (with a warning if a
module still has more).

**Incremental sync.** Every list call sends `If-Modified-Since: <cursor>`. Zoho answers `304 Not
Modified` when nothing changed, so a quiet CRM costs a handful of requests. The filter is
inclusive, so a record changed exactly at the cursor time is read again; `merge` makes that
harmless. Each module (and Notes, Attachments) has its own cursor: the UTC time the previous run
**started**, kept in dlt resource state and advanced only after a fully successful run. Paging
follows `next_page_token`, so there is no 2,000-record ceiling.

**Forget-on-delete.** On each incremental run the connector reads Zoho's Deleted Records API
(`/<module>/deleted?type=all`: recycle bin and permanently deleted). Records, notes and attachments
deleted since the cursor become `{"id": ..., "_deleted": True}` tombstones. Notes and attachments of
a deleted record are tombstoned too (the connector remembers each child's parent). dlt removes
tombstoned rows on `merge`, and cognee's `orphan_cleanup` removes them from the graph and vector
stores.

**Failures.** `429`, `5xx` and network errors are retried with backoff. Any other error aborts the
run: no cursor moves and nothing is forgotten, so a partial read can never cause a false deletion.

### Limits

* Zoho meters API use in credits per day, by edition. A first sync costs about one request per 200
  records per module, plus the field metadata calls; later runs are mostly `304` answers.
* A record restored from the recycle bin returns on the next run only if Zoho updates its modified
  time. Pass `modified_since` with an old date for a one-off full re-read.

## Privacy

CRM data is customer data. Nothing is fetched until you run the connector. The scope above is
read-only, contact details are left out by default, and you can limit `modules`. Use a dedicated
dataset so you can remove it with one `cognee.prune`.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake Zoho CRM v8 API (no credentials, no network) that mirrors behaviour seen on a
live account: mandatory `fields` capped at 50, inclusive `If-Modified-Since` with `304`, `204` for an
empty deleted list, `next_page_token` paging, and `invalid_client` for a wrong region. They cover
field selection and redaction, stable rows, OAuth refresh and region errors, rate-limit retries,
paging, the per-module cursor, forget-on-delete for records, notes (directly and via a deleted
parent) and attachments, text-only attachments with a size cap, and a failed run that keeps the
cursor.
