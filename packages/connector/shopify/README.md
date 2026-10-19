# cognee-community-connector-shopify

A Shopify data-source connector for [cognee](https://github.com/topoteretes/cognee): sync a
store's products and collections, and opt-in its customers and orders, into memory and ask
questions about them.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Every product, collection,
customer and order is ingested as a **normal document**, so it goes through cognee's cognify
entity extraction. Re-running syncs only what changed, and anything deleted in Shopify is
forgotten on the next sync.

## Install

```bash
uv pip install cognee-community-connector-shopify
# or, from this monorepo:
cd packages/connector/shopify && uv sync --all-extras
```

The package needs `cognee>=1.6.2,<2`: document-mode routing, the table-scoped orphan cleanup,
`PIPELINE_SCOPE_ATTR` and `guarded_rows` all ship in cognee 1.6.2.

## Setup: a free dev store

1. Sign in to the [Shopify Dev Dashboard](https://dev.shopify.com/dashboard) and create (or
   pick) an organization.
2. **In the Dev Dashboard**, create a development store in that organization. Choose "start
   with test data" if offered. A store created from the Shopify admin instead cannot use the
   app below: the token request fails with `shop_not_permitted`.
3. In the same organization, create an app. In its version settings, give it the Admin API
   scopes `read_products`, plus `read_customers` and `read_orders` if you want those, and
   release the version.
4. Install the app on the dev store.
5. Copy the app's client id and client secret, and export them with the store name:

   ```bash
   export SHOPIFY_SHOP="your-store"          # or your-store.myshopify.com
   export SHOPIFY_CLIENT_ID="..."
   export SHOPIFY_CLIENT_SECRET="..."
   export LLM_API_KEY="..."                  # like any other cognee run
   ```

   A store that still has a static Admin API access token can set `SHOPIFY_ACCESS_TOKEN`
   instead of the client id and secret.

The connector is read-only. It only sends GraphQL queries, never mutations. The credentials
are checked before the first request: one with whitespace or control characters in it (a
pasted newline, say) is refused with a `ValueError` that does not repeat it.

Ingested text goes to the LLM configured for cognee: cognify sends each document to it for
entity extraction.

## Usage

```python
import cognee
from cognee_community_connector_shopify import shopify_source

await cognee.remember(
    shopify_source(),  # SHOPIFY_SHOP and credentials from env
    dataset_name="shopify",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by row id
    max_rows_per_table=0,  # unlimited: orphan cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="Which products are in the summer collection?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["shopify"],
)
```

Re-running `remember(...)` with the same dataset syncs only what changed and forgets what was
deleted. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required.** The add pipeline defaults to `"replace"`,
> which would drop every document that is not re-emitted on the second sync.

### Selecting what to ingest

| Argument | Meaning |
|---|---|
| `shop="your-store"` | The store, as its name or `name.myshopify.com`. Defaults to `SHOPIFY_SHOP`. Custom domains are refused: credentials only ever go to the `myshopify.com` host. |
| `resources=("products", "collections")` | What to sync. Add `"customers"` and `"orders"` to include those; they hold personal data and are off by default. |
| `client_id=`, `client_secret=` | App credentials, exchanged for a token at the start of every run. Default to `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET`. |
| `access_token=` | A static Admin API token, used instead. Defaults to `SHOPIFY_ACCESS_TOKEN`. |
| `resource_name="shopify_documents"` | The dlt table name, which also scopes the sync state. Lowercase snake_case, at most 64 characters. See below. |
| `check_active=None` | Optional callable for a host: asked before every Shopify request and every wait, and around every row; raising stops the sync with nothing deleted. |
| `service=None` | A pre-built `requests`-like session (mainly for tests); `session=` is accepted too. Without it, each run builds its own session and closes it. |

**Several stores in one dataset.** Each `resource_name` has its own table and its own sync
state, and a sync only ever forgets documents of its own table. Two stores (or two
selections) in one dataset therefore need two names, for example `resource_name="shopify_eu"`
and `resource_name="shopify_us"`. With the same name, each run would forget the other's
documents. Removing a resource from `resources` forgets its documents.

### What gets stored

One document per product, collection and, if selected, customer and order:

```text
# Linen shirt

Type: product
Status: active
Vendor: Acme
Product type: Shirt
Tags: linen, summer
Handle: linen-shirt
Option Size: S, M

Breathable linen.

Variants:
- S / Sand, SKU LS-S, price 49.00
- M / Sand, SKU LS-M, price 49.00
```

- **Product:** title, status, vendor, product type, tags, handle, online store URL, options,
  the plain-text description, and one line per variant (title, SKU, price), at most 100.
  **No inventory quantities**: they change with every sale.
- **Collection:** title, custom or smart (with its rules), handle, description, and its
  products by title, at most 250.
- **Customer** (opt-in): name, email, state, tags, note, and the city and country of the
  default address. **No phone number, no street address, no order counts or amounts spent.**
- **Order** (opt-in): name (`#1001`), payment and fulfillment status, total, the customer by
  name and id (`Customer: Ada Lovelace (customer:5)`), and each line item's quantity and
  product by title and id.

Orders name their customer and products, and collections name their products, so cognee's
entity extraction links them to the customer and product documents. Lists are sorted (tags,
options, variants by position, members by title), and no text carries timestamps or
counters, so an unchanged item keeps its content hash and is not re-cognified.

**Who can read it.** Anyone who can read the cognee dataset can read these documents,
customer names and emails included when customers or orders are selected. Give the dataset
only to people who may see that data.

## How incremental sync works

- **Products and customers.** Every run lists all ids with their `updatedAt` (the sweep, 250
  per request). Only those whose `updatedAt` differs from the last settled copy are fetched
  in full. A copy fetched within 60 seconds of its `updatedAt` is fetched once more on the
  next run, in case a second change carried the same timestamp.
- **Collections.** A collection shows its products by title, and neither a membership change
  nor a product rename can be told from the collection listing. So every collection is read
  each run (one request each) and emitted only when its text changed.
- **Orders.** Listed by `updated_at` from shortly before the last newest one. An order also
  shows its customer and products by name: each order keeps the ids it shows and a hash of
  those names, and when a sweep shows one of them renamed, the order is fetched again.

## How forget-on-delete works

Products, collections and customers are swept; orders are checked by id inside the 60-day
window (below).

For the swept kinds, the sweep is the authority. An id that was a document before and is
missing from it is forgotten only when Shopify confirms it is gone, either way:

- a `destroy` event for it, read from the events feed since the last run; or
- `nodes(ids:)` answering `null` for it.

An id that still resolves, or whose check fails, keeps its document and is asked about again
on the next run.

Orders are never swept: without `read_all_orders` only the last 60 days are visible, and an
order ageing out of that window is not a deletion. Shopify records a `destroy` event at once
for a deleted product or collection, but recorded none for a deleted order (seen live). So
each known order that the run did not list is asked about with `nodes(ids:)` while it was
created more than 7 days inside the window, and forgotten when it answers `null`. What
`nodes(ids:)` answers for an order past the window was not observable, so older orders are
not asked about. The window is measured on Shopify's clock (the `Date` header of the run's
first answer), not the local one. An order's `destroy` event is still honoured, in case
Shopify ever records one.

Forgotten ids are emitted with the `_deleted` hard-delete marker, dlt removes those rows on
`merge`, and cognee's `orphan_cleanup` removes them from the graph and vector stores.

An id missing from the sweep that still resolves in three runs in a row is forgotten as
well: it is no longer listed, so it is no longer part of what the source syncs.

All listings are completed before the first row is emitted, and state is saved only after the
last one. If a listing fails (after retries), or `check_active` raises, the run aborts:
nothing is deleted and no cursor moves. A `200` whose body is not JSON, has no `data`, or has
a connection without `pageInfo` also aborts, whether it came from a listing or from one
item; it is never read as an empty listing or an item to skip.

## When Shopify fails part of the way through

Only the sweep decides that an item is gone. An item that is still listed but fails on its
own keeps its previous document and is fetched again on the next run; the rest of the run
goes on:

- **Unreadable** (`404`, or `null` for an item that is still listed, or `403` while other
  items of its kind are read in the same run): kept. If it stays that way in three runs in a
  row at the same `updatedAt`, its document is forgotten and the item is not asked about
  again until its `updatedAt` moves. A `403` on every item of a kind is a permission problem:
  kept, counted as `failed_permission`, and never moves toward forgetting.
- **A GraphQL `ACCESS_DENIED`** on any query aborts the run before anything is saved, with an
  error naming the field the app may not read.
- **A server error or no answer** that outlasts the retries: kept. Three of these in a row on
  fresh items means Shopify itself is down, and the run stops cleanly (below).

Items kept from an earlier run are fetched after the fresh ones and never count toward that
streak, so items that keep failing cannot hold up the others.

Some failures stop the run **cleanly** instead of aborting it: a `401` after the one token
refresh once at least one document was emitted, a throttle or `429` that outlasts the retries
or asks for a wait over 60 seconds, and the outage above. The run then ends normally: what it
emitted is loaded, and nothing is deleted. The order and destroy-event cursors do not move,
so the next run lists those again; products and customers not reached keep their old stamp
and collections are read every run, so the next run fetches the rest even if nothing changed
upstream. A `401` before any document was emitted still raises, since the credentials are bad.

Error messages name the status (or GraphQL code) and the operation only
(`Shopify Product: HTTP 503`). Shopify's own explanation is on the exception's `detail`; a
response body or a credential never is. Retry warnings name the error type only.

### Sync stats

`resource.cognee_sync_stats` holds the counts of the last run, as plain ints: `scanned` (items
listed), `skipped` (unchanged), `failed` (item failures), `deleted`, `kept` (items waiting for a
retry, and missing ids still resolving) and `given_up`, plus `failed_auth`,
`failed_rate_limit` or `failed_unavailable` when the run stopped early for that reason, and
`failed_permission` (how many items answered `403` in a kind where none could be read).

## Tokens and rate limits

With a client id and secret, the connector requests a token
(`POST /admin/oauth/access_token`, the client credentials grant) at the start of every run
and keeps it in memory only: never in dlt state, files or logs. On a `401` it requests a new
one once and repeats the request; a second `401` stops the run.

Shopify limits GraphQL by query cost. The connector reads the `throttleStatus` Shopify returns
with every answer and waits before a query the remaining budget cannot pay for. Every query
stays under the 1,000-point limit for a single query (a test checks each one); if Shopify
still refuses one as too costly (`MAX_COST_EXCEEDED`), the run stops with an error naming the
query. The API version is pinned (`2026-10`); if Shopify answers from another one, a warning
says so. A `THROTTLED` error or an HTTP `429` is waited out and retried, as are 5xx answers
and network errors, up to five attempts. No single wait is longer than 60 seconds: cognee
holds its staging lock during a sync, so a longer `Retry-After` stops the run cleanly instead.

## Limits

- Anyone who can read the cognee dataset can read what was ingested (see "What gets stored").
- Two stores or selections in one dataset need different `resource_name`s.
- Only orders from the last 60 days are visible without the `read_all_orders` scope. Orders
  older than 60 days stay in memory as last seen and do not follow renames of their customer
  or products.
- Every collection costs one request per run. A store with thousands of collections takes as
  many requests each run.
- Product variants are cut at 100, collection members at 250 and order line items at 50 (to
  keep each query under Shopify's 1,000-point cost limit); the document always says so, with
  the number of the rest where Shopify reports a total.
- Deleted products, collections and customers are confirmed through the destroy events or
  `nodes(ids:)`. If the token may not read events, the connector logs one warning and relies
  on `nodes(ids:)` alone.
- An order deleted in its last 7 days inside the window, or after it, is never forgotten.
- Checking orders costs one `nodes(ids:)` request per 250 known orders from the window, each
  run, at 1 cost point per request (measured live): a store with 10,000 orders in 60 days
  makes 40 extra requests and waits for no throttle.
- Bulk operations are not used, so a first sync of a large store takes one request per
  product. Webhooks are not used either: changes are picked up on the next sync.
- An item that stays unreadable for three runs in a row (a `404`, a `null` while listed, or a
  `403` while others of its kind are read) is forgotten until it changes. A GraphQL
  `ACCESS_DENIED` on any query (a field or scope the app may not read) aborts the run with an
  error naming the field instead: it is the app's access, not one item. When every fetch of a
  kind answers `403` in a run, nothing moves toward forgetting; the run counts them as
  `failed_permission` and logs one warning per kind.
- Metafields, images, inventory, discounts, draft orders and blog content are not ingested.
- Deletions propagate on a foreground `remember(...)`. cognee skips orphan cleanup for
  `run_in_background=True`.

## Verified against a real store vs. taken from the docs

Checked against a Dev Dashboard development store with Shopify's test data, API version
`2026-10`, on 2026-10-17. Changes were made in the Shopify admin on items created for the
checks.

- The client credentials grant answers `{access_token, scope, expires_in: 86399}`; a wrong
  secret answers `400 invalid_request`; an invalid token answers `401` on GraphQL.
- Every answer carries `X-Shopify-API-Version: 2026-10`. Every query the connector sends is
  accepted, and each costs at most 74 points (orders), far under the 1,000-point limit.
- What moves a product's `updatedAt`: an inventory change, a price edit, a rename, adding or
  deleting a variant. Adding the product to a collection does not. That is why inventory is
  kept out of the text (a sale re-fetches the product, but its text and hash stay the same),
  and why product documents do not list their collections.
- A collection's `updatedAt` moves on a membership change, a member's rename or price edit,
  and a member's deletion; the connector reads collections every run regardless.
- Timestamps are whole seconds and appear within seconds of the save.
- A deleted product shows at once as a `destroy` event (`subjectType: PRODUCT`, its own id),
  and `nodes(ids:)` answers `null` for it. Variant deletes show as `PRODUCT_VARIANT` events
  and are ignored (the product moves anyway). Archived and draft products stay listed.
- `events` needs no scope beyond the three above.
- The connector through a dlt pipeline: a first sync, a quiet sync (nothing fetched), a
  deleted product (forgotten through its event, and its collection re-rendered without it), a
  deleted collection (forgotten), and quiet again. No token reaches the dlt state.
- Orders (with `read_orders`): an order lists with `customer { id }` and its line items; with
  customers not selected, the order names its customer by id.
- **Protected customer data is granted per field.** With customer access but without the
  Name field, Shopify answers `ACCESS_DENIED` "This app is not approved to use the
  displayName field". The connector stops with an error naming the Name and Email fields.
- With the Name and Email fields granted: customers sync, and an order names its customer
  (`Customer: Probe Customer (customer:...)`). Renaming the customer re-rendered the customer
  and the order and nothing else. Cancelling the order re-rendered it as refunded.
- **Deleting an order leaves no `destroy` event**: Shopify recorded `cancelled`, `closed` and
  refund events for it, but no `destroy` event, still none about 8 minutes later.
  `nodes(ids:)` answers `null` for it, and the next sync forgot it that way.
- `nodes(ids:)` costs 1 point whether it is asked about 1, 50 or 250 ids.

Taken from Shopify's documentation, not reproduced live:

- `THROTTLED` errors and HTTP `429` (the budget was never close to empty); covered by the
  mocked tests.
- What an app with no customer access at all sees on an order: access was granted before the
  first order existed.
- Orders older than 60 days needing `read_all_orders`.

## Testing

```bash
uv run pytest tests/ -q
```

The tests mock the GraphQL Admin API (no store, no network). They cover rendering (also from
shuffled input), the shop name and URL guard, `errors` with HTTP 200, throttling and `429`
(and a wait over 60 seconds not taken), bodies of the wrong shape and a missing `pageInfo`,
change detection, product renames re-rendering collections and orders, membership changes,
deletion through events and `nodes()`, ids kept when not confirmed gone and given up after
three runs, orders ageing out versus deleted (with or without a destroy event), token
refresh, the token never reaching state, logs or an error message, items kept and retried,
three unreadable items not blocking a run and given up after three runs, retries going last,
and the clean stops (`401` mid-run, throttle, long `Retry-After`, outage) with what the next
run picks up. A real `dlt` pipeline into a temp sqlite destination checks edits and deletes
across runs (the deleted row is gone from the table), two sources in one dataset staying
apart, a `401` mid-run resuming on the next run, and `check_active` stopping a run with
nothing loaded.
