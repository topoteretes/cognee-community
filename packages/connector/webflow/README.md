# cognee-community-connector-webflow

A Webflow data-source connector for [cognee](https://github.com/topoteretes/cognee): sync the CMS
collections of a Webflow site (and optionally its static pages) into memory, and forget content
when it is deleted or unpublished upstream.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each CMS item becomes a short
**document** (a header, then one `Label: value` line per field, rich text rendered as plain text)
that flows through cognify. Each collection also gets a **collection card** listing its fields,
types, options and help text, so the LLM knows what the fields mean. A collection's items and its
card share a node set (`webflow:<site>/<collection>`), so they are linked in the graph.

## Requirements

- cognee **1.6.4** (pinned). The connector relies on document mode with table-scoped cleanup
  and per-row node sets (`DOCUMENT_SYNC_VERSION` 2).
- Python 3.11 to 3.13.
- A Webflow site and an API token (see [Setup](#setup)).

## Install

```bash
uv pip install cognee-community-connector-webflow
# or, from this monorepo:
cd packages/connector/webflow && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_webflow import webflow_source

source = webflow_source(
    collections=["blog-posts", "team"],  # slugs, ids or display names; omit for every collection
    include_pages=True,  # optional: static page text, needs the pages:read scope
)  # the token comes from WEBFLOW_API_TOKEN, or pass token="..."

await cognee.remember(
    source,
    dataset_name="webflow",
    primary_key="id",
    write_disposition="merge",  # required: incremental runs emit only changes and deletions
)

answer = await cognee.recall("What did we publish about pricing?", datasets=["webflow"])
```

Run the same `remember` call again whenever you want to sync. See `examples/example.py` for the
full flow.

| Argument | Meaning |
|---|---|
| `token` | A site token or an OAuth access token. Falls back to `WEBFLOW_API_TOKEN`. |
| `site_ids` | Sites to sync. Default: every site the token can see (a site token sees exactly one). |
| `collections` | Collections to sync, each an id, slug or display name. Default: every collection of the selected sites. |
| `include_pages` | Also sync the text of static pages. Default `False`. CMS template pages are skipped, their content is the CMS items. |
| `include_drafts` | Sync the staged version of items (drafts and unpublished edits) instead of only what is published. Default `False`. |
| `include_collection_cards` | Also ingest one card per collection. Default `True`. |
| `full_refresh` | Send every item this run, not only changed ones. |
| `resource_name` | Name for this connection's dlt state and staging tables. Default `webflow`; give each Webflow account its own when several sync into one dataset. |
| `client` | A `WebflowClient`, for a custom base URL, transport or retry behaviour. |

After a run, `source.cognee_sync_stats` holds counts only (items, unchanged, deleted, pages,
collections, skipped), never content.

## Setup

### 1. Get a token

**Site token** (simplest, one site): in Webflow open *Site settings > Apps & integrations > API
access > Generate API token*, name it and pick the scopes below. Only site administrators can
create one, and it is revoked from the same place.

**OAuth** (several sites, or an app you distribute): register a Data Client app and run
Webflow's [authorization code flow](https://developers.webflow.com/data/reference/oauth-app).
The access token it returns works exactly like a site token: pass it as `token=`. The connector
does not run the OAuth flow for you.

| Scope | Needed for |
|---|---|
| `cms:read` | collections and their items (always) |
| `sites:read` | listing sites and reading site names (optional when you pass `site_ids`) |
| `pages:read` | `include_pages=True` |

The connector only ever reads. The token is sent as a bearer header to `api.webflow.com` and is
never logged or included in an error message.

### 2. Choose what to sync

By default, every collection of every site the token can see. Narrow it with `site_ids=[...]` and
`collections=[...]`. A name in `collections` that matches nothing raises an error instead of being
skipped, because skipping it would forget what it synced before. Remove it from the list to forget
its content on purpose.

## How a sync works

- **What is read.** Published items by default (`/items/live`). With `include_drafts=True` the
  staged items, drafts included; archived items are always out of scope. A site that has never
  been published has no live items (Webflow answers 409), so the run stops with a hint: publish
  the site once, or use `include_drafts=True`. It never reads this as "no items", which would
  forget everything.
- **Incremental sync.** Webflow's list endpoints return whole items and do not report deletions, so each run lists
  the selected collections (100 items per request) and compares each item's `lastUpdated` with
  the value kept from the last run. Only new or changed items are sent on; an unchanged item is
  neither re-sent nor re-read by the LLM. An item whose text did not change but whose
  `lastUpdated` moved (a save, a publish) is sent again with an identical document, which
  cognee's content hash skips. Changing a collection's fields or options re-renders its items.
  Pages cost one more request each for their content, so only changed pages are read.
- **Forget-on-delete.** An item synced before that is no longer in the listing (deleted,
  unpublished, or archived) is removed from the graph and vector stores on the next sync. So is
  everything in a collection or site that was deleted, or taken out of your selection. Turning
  `include_pages` off forgets the pages.
- **Errors never delete.** A failed request, a listing that is incomplete or that kept changing
  while it was read, or a page that Webflow server-errors on stops the run before anything is
  committed, so nothing is forgotten and the next run starts from the same state. A suspected
  deletion is confirmed by reading the collection a second time. A page whose content the token
  cannot read (403/404) is skipped and kept, never forgotten.
- **Rate limits.** Webflow allows 60 requests a minute on Starter and Basic plans and 120 on CMS
  and above. A 429 is retried after its `Retry-After`, and 5xx and network errors are retried with
  backoff. As a guide, a collection of 1,000 items costs about 12 requests per run.

## Privacy

This reads the content of the items you select and sends it, as text, to your configured LLM and
embedding providers during cognify. Select only the collections you mean to, leave
`include_drafts` off unless you want unpublished content in memory, and use a dedicated dataset
so you can forget it in one call.

## Limitations

- Reference and multi-reference fields, and files, are not rendered: they hold ids, not text.
  Images contribute their alt text.
- Localized variants of an item (secondary locales) are not synced separately; the first variant
  returned for an item id is kept.
- Page content comes from Webflow's page DOM endpoint, which covers text, image alt text and
  component text on static pages. Availability depends on your plan and the token's scopes; if
  `pages:read` is missing the run stops with Webflow's error, so leave `include_pages` off in
  that case.
- Webflow's changes are detected by polling. Run the sync as often as you need it.

## Tests

```bash
cd packages/connector/webflow
uv sync
uv run pytest
```

No Webflow account or API key is needed. The tests run against an in-memory fake of the Webflow
Data API v2, served through `httpx`:

- **Client:** auth, retries, `Retry-After`, pagination, and listings that change while read.
- **Sync logic:** ingest, the `lastUpdated` cursor, forget-on-delete and the errors-never-delete
  rules, for items, collection cards and pages.
- **dlt:** a real dlt pipeline checks that tombstones remove staged rows and that state survives
  between runs.
- **End-to-end:** the real cognee pipeline (with a mocked LLM and embeddings) checks that deleted
  and unpublished items and deleted collections leave the graph, and that an unchanged re-sync
  makes no LLM calls.

To try it against a real site, run `examples/example.py` with a site token.
