# cognee-community-connector-freshsales

A Freshsales (Freshworks CRM) data-source connector for [cognee](https://github.com/topoteretes/cognee):
turn your CRM into memory - "which deals in negotiation got a discount, and who is the contact?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Contacts, accounts and deals, each
with its notes, are ingested as **normal documents** (cognee document-mode), so they go through
cognify entity extraction.

## Requirements

cognee **>= 1.4.0** (document-mode: `DOCUMENT_SOURCE_ATTR`).

## Install

```bash
uv pip install cognee-community-connector-freshsales
# or, from this monorepo:
cd packages/connector/freshsales && uv sync --all-extras
```

## Setup

1. In Freshsales, open your profile -> **Settings** -> **API Settings** and copy your **API key**.
2. Note your **domain**: the part before `.myfreshworks.com` in the browser address bar
   (`https://acme.myfreshworks.com/crm/sales` -> `acme`). Older `acme.freshsales.io` accounts work too.
3. Export them and your LLM key:

```bash
export FRESHSALES_DOMAIN="acme"
export FRESHSALES_API_KEY="..."
export LLM_API_KEY="sk-..."
```

## Usage

```python
import cognee
from cognee_community_connector_freshsales import freshsales_source

await cognee.remember(
    freshsales_source(),  # or freshsales_source(entities=["deals"])
    dataset_name="freshsales",
    write_disposition="merge",  # REQUIRED, see below
)

answer = await cognee.search(
    query_text="Which deals are in negotiation and what do the notes say about pricing?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["freshsales"],
)
```

| Argument | Meaning |
| --- | --- |
| `domain` | `acme`, `acme.myfreshworks.com` or a full URL. Default: `FRESHSALES_DOMAIN`. |
| `api_key` | API key. Default: `FRESHSALES_API_KEY`. |
| `entities` | Any of `contacts`, `accounts`, `deals`. Default: all three. |
| `include_notes` | Add each record's notes to its document (default `True`). |
| `redact_contact_details` | Leave out e-mail and phone fields (default `True`). |
| `reconcile` | Detect records purged from the Recycle Bin (default `True`, see cost below). |
| `modified_since` | ISO 8601 time that limits the **first** run only. |

> **`write_disposition="merge"` is required.** cognee defaults to `replace`, which reloads the
> table each run. An incremental run only sees what changed, so `replace` would forget the rest.

See `examples/example.py` for a runnable script.

## What a document contains

* **Contact** (`id = contacts:<id>`): name, job title, status, lifecycle stage, accounts, owner,
  location, tags, created time, description, notes.
* **Account** (`id = accounts:<id>`): name, industry, business type, website, employees, annual
  revenue, contacts, owner, location, tags, created time, description, notes.
* **Deal** (`id = deals:<id>`): name, amount, stage, pipeline, probability, expected close, closed
  date, type, lost reason, account, contacts, owner, tags, created time, notes.

Ids (stage, owner, account, contacts, ...) are resolved to names. Notes are listed oldest-first as
`- [time by author] text`.

**Built from an allow-list.** Raw records are never dumped into documents. A live Freshsales deal
record carries chat-widget **JWT auth tokens** (`fc_widget_collaboration`), which must never reach a
knowledge graph. Also left out on purpose:

* e-mail and phone fields, unless `redact_contact_details=False`;
* derived or volatile values that change without the record being edited (deal age, lead score,
  open/won totals, assignment and stage-change times). Leaving them out keeps the content hash
  stable, so a record is re-cognified only when its real content changes.

## How it works

**Incremental sync.** Each entity is searched with
`POST /filtered_search/<entity>` and the rule `updated_at is_after <cursor>`, sorted by
`updated_at`, 100 per page. On a live account:

* `is_after` is **strict** and timestamps have one-second precision, so the query starts a
  5-minute safety window before the cursor. `merge` makes the overlap harmless.
* An **unknown operator does not fail**: it is silently treated as "equals" and returns almost
  nothing. The operator is fixed in code for that reason.

The cursor is the UTC time the previous run **started**, kept per entity in dlt resource state and
advanced only after a fully successful run.

**Full records.** Search results are partial (a deal has no stage, owner or even `updated_at`), so
each *changed* record is read with one detail call that embeds its notes, owner, stage, account and
contacts (`?include=...`). Unchanged records cost nothing.

**Forget-on-delete.** Deleted records move to each entity's **Recycle Bin** view. Every incremental
run reads it and tombstones those ids. With `reconcile=True` (default) the connector also lists all
ids of the entity (one request per 100 records) and tombstones known ids that are gone, which also
covers records purged from the Recycle Bin. Tombstones are `{"id": ..., "_deleted": True}` rows; dlt
removes them on `merge`, and cognee's `orphan_cleanup` removes them from the graph and vector stores.

**Rate limits.** The live account reports **5,000 requests/hour** and **400/minute** in its
headers. The connector pauses when the minute budget is almost used up (`per-min-x-ratelimit-remaining`)
and retries `429`, `5xx` and network errors. Any other error aborts the run: no cursor moves and
nothing is forgotten, so a partial read can never cause a false deletion.

**Cost.** First sync: about one request per record, plus one per 100 records. A run with no changes:
about 4 requests per entity.

## Privacy

CRM data is customer data. Nothing is fetched until you run the connector. Contact details are left
out by default, and you can limit `entities`. Use a dedicated dataset so you can remove it with one
`cognee.prune`.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake Freshsales API (no credentials, no network) that mirrors the live behaviour
above: partial search results, strict `is_after`, unknown operators acting as "equals", `?include=`
detail calls, Recycle Bin views and a deal carrying JWT tokens. They cover URL handling, the safety
window, document allow-listing (no tokens, no contact details, no volatile values), paging, the
per-entity cursor, forget-on-delete through the Recycle Bin and through reconcile, entity selection,
rate-limit retries and minute-budget pausing, and a failed run that keeps the cursor.
