# cognee-community-connector-woocommerce

A WooCommerce data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync a store's products, product reviews and orders into memory, so an agent can answer
"what do reviews say about sizing on the linen shirt?" or "what did this customer ask for
on their last order?".

It exposes a `dlt` resource you hand to `cognee.remember(...)` / `cognee.add(...)`. Every
record becomes a **normal document** that goes through cognify's entity extraction (via
cognee's document mode), not the deterministic dlt-row path. Syncs are incremental and
forget-on-delete.

## Requirements

- cognee **1.6.3** or later 1.x (document mode, `PIPELINE_SCOPE_ATTR`, `guarded_rows` and
  the `cognee_node_set` column). The source checks this and refuses older releases.
- WooCommerce with the REST API (`/wp-json/wc/v3`) and pretty permalinks. Tested against
  WooCommerce 11.2.1 on WordPress 7.1.3, with both order storages (HPOS on and off).
- A REST API key with **Read** permission.

## Install

```bash
uv pip install cognee-community-connector-woocommerce
# or, from this monorepo:
cd packages/connector/woocommerce && uv sync --group dev
```

## Usage

```python
import cognee
from cognee_community_connector_woocommerce import woocommerce_source

source = woocommerce_source()  # WOOCOMMERCE_STORE_URL / _CONSUMER_KEY / _CONSUMER_SECRET
await cognee.remember(
    source,
    dataset_name="store",
    primary_key="id",
    write_disposition="merge",  # required, see below
    max_rows_per_table=0,  # no row cap, so cleanup sees the whole corpus
)
print(source.cognee_sync_stats)

results = await cognee.recall("Which products get complaints about shipping?", datasets=["store"])
```

`write_disposition="merge"` is required: `remember()` defaults to `"replace"`, which would
drop every record an incremental run does not re-emit.

### Options

| Argument | Default | What it does |
|---|---|---|
| `store_url`, `consumer_key`, `consumer_secret` | env vars | The store and a **Read** REST API key. Fall back to `WOOCOMMERCE_STORE_URL`, `WOOCOMMERCE_CONSUMER_KEY`, `WOOCOMMERCE_CONSUMER_SECRET`. |
| `resources` | `("products", "reviews", "orders")` | Any of `products`, `reviews`, `orders`, `customers`. Customers are opt-in. |
| `product_status` | `"publish"` | Which products are in scope; `"any"` means every status but trash. |
| `include_variations` | `False` | Add a variable product's variations (attributes, price, SKU) to its document. |
| `include_private_notes` | `False` | Add private order notes written by staff, not just the notes sent to the customer. |
| `include_reviewer_names` | `False` | Add the reviewer's display name to reviews. |
| `order_notes_window_days` | `7` | Orders modified within this many days are re-read on every run (see below). `0` turns it off. |
| `resource_name` | `"woocommerce_documents"` | The dlt table name, which also scopes the sync state. Give each store its own name when several share a dataset. |
| `check_active` | `None` | Host authorization checkpoint, called before every request and around every row. Raise to stop the sync. |
| `service` (alias `session`) | `None` | A `requests`-like session, mainly for tests. Without one, a session is built per run and closed after it. |

## What is stored

One document per record, each in its own node set (`products`, `reviews`, `orders`,
`customers`), with ids like `product:12`, `review:7`, `order:31`:

- **Products:** name, SKU, type, categories, tags, price (sale and regular), attributes,
  short and long description (HTML turned into text), and optionally variations.
- **Reviews:** approved reviews only, with the product name, rating, whether the reviewer
  is a verified owner, the date and the text. A review follows its own status, not its
  product's: trashing a product forgets its reviews (WooCommerce marks them
  `post-trashed`), but unpublishing it does not.
- **Orders:** number, status, date, total, discount, shipping method, coupons, line items
  (name, quantity, SKU, line total), the note the customer left at checkout, and order
  notes: notes sent to the customer by default, staff-written private notes on request.
  Notes WooCommerce writes itself ("Order status changed…", email failures) are skipped.
- **Customers (opt-in):** name, company, city and country, registration date.

### Privacy

- Never stored: billing and shipping addresses, emails and phone numbers (on orders,
  reviews and customers alike). Orders refer to their customer by id only.
- Opt-in: customers, private order notes, reviewer names.
- The connector only reads. A **Read** key is all it needs.

### Left out on purpose

Stock levels, sales counts, ratings averages and modification times change all the time.
Every document column feeds cognee's content hash, so including them would re-cognify a
product on every sale. They are not rendered.

## How sync + forget-on-delete work

Each run has four steps, and the state is only written at the end. A run that fails
partway leaves the state as it was, so the next run starts from the same place.

1. **List.** Products and orders are listed with `modified_after` (the previous run's
   newest `date_modified_gmt`, minus a 60 second overlap), `orderby=modified` and
   `dates_are_gmt=true`. Reviews and customers have no modified filter in the API, so
   they are listed in full. Every listing follows `X-WP-TotalPages`, and a page without
   that header, or a body that is not a list of records, aborts the run.
2. **Confirm deletions.** Products and orders are also listed by id (`_fields=id,status`)
   to find ids that left the selection. Each one is fetched by id before it is forgotten.
   It counts as gone only when the store answers 404, or answers with a status outside the
   selection: trashed, unpublished, a review marked as spam, or the review of a trashed
   product (`post-trashed`). One that still answers in scope is kept. Any other failure
   aborts the run, so a flaky listing can never delete anything. This costs one request
   per departed record, so a run right after a mass delete takes longer.

   WooCommerce pages by offset, so a record deleted mid-listing would shift the next page
   by one and a record could be skipped. Every page's `X-WP-Total` is compared with the
   first page's, and a listing whose total moved is read again from page one.
3. **Render.** Each new or changed record becomes a document. A fingerprint of the rendered
   text is kept in the state, and a record whose text did not change is skipped, so the
   overlap and the full review listing never re-ingest anything. A record that fails to
   load (a 5xx after retries) is kept and retried next run. Three in a row stop the run as
   an outage, and a 401 or 403 stops it at once.
4. **Delete and commit.** Confirmed deletions are emitted as `_deleted` rows (dlt's
   hard-delete column), and cognee's orphan cleanup removes their documents, graph nodes
   and vectors. A resource dropped from `resources` is forgotten the same way.

**Order notes.** Adding a note to an order does not change the order's `date_modified`, so
the modified cursor alone would never see it. Orders modified within the last
`order_notes_window_days` (default 7) are therefore re-read on every run, which costs one
notes request per recently active order. Fingerprints keep the unchanged ones from being
re-ingested. With `0`, a note is picked up the next time its order changes.

**Rate limits and waits.** WooCommerce has no rate limit of its own, but hosts often add
one. 429 and 5xx answers and network errors are retried up to 5 times with `Retry-After`
or exponential backoff. cognee holds its dlt staging lock during a sync, so a single wait
over 60 s, or more than 120 s of waiting in one run, ends the run instead.

**Errors and secrets.** Error messages name only the HTTP status and the endpoint template
(`/orders/{id}`). They never contain a record id, the key or a response body.
WooCommerce's error code is on `WooCommerceAPIError.detail`. Keys containing whitespace or
control characters are refused up front.

**One store per `resource_name`.** The state remembers the store URL, and a second store
under the same `resource_name` is refused rather than having the first store's records
read as deleted.

### Stats

`source.cognee_sync_stats` holds the last run's counts: `listed`, `rendered`, `unchanged`,
`deleted`, `kept` (records retried next run) and `failed`.

## Setup

1. In WordPress, open **WooCommerce > Settings > Advanced > REST API** and add a key with
   **Read** permission for a user who can view the store.
2. Export it, along with the store's address:

   ```bash
   export WOOCOMMERCE_STORE_URL="https://shop.example.com"
   export WOOCOMMERCE_CONSUMER_KEY="ck_..."
   export WOOCOMMERCE_CONSUMER_SECRET="cs_..."
   ```

**HTTPS and HTTP.** Over `https://` the key is sent as HTTP basic auth. WooCommerce only
accepts keys over plain `http://` (a local store) through one-legged OAuth 1.0a, so the
connector signs every request (HMAC-SHA256, a fresh nonce per attempt). Plain HTTP to
anything but localhost logs a warning: requests are signed, but the store's data travels
unencrypted.

### A local test store

`examples/local_store` starts WordPress, WooCommerce and MariaDB in Docker, seeds a few
products, reviews, a customer and orders with notes, and prints a Read key:

```bash
cd examples/local_store
docker compose up -d
sh seed.sh            # prints WOOCOMMERCE_STORE_URL / _CONSUMER_KEY / _CONSUMER_SECRET
```

The store is at http://localhost:8088 (admin / admin). To try the newer order storage, run
`docker compose exec cli wp wc hpos sync` and then `docker compose exec cli wp wc hpos enable`.

Then run the example twice, changing something in the store in between:

```bash
uv run python examples/example.py
```

## Testing

```bash
uv sync --group dev
uv run pytest tests
```

The tests need no key and no network:

- `tests/fake_woocommerce.py` is an in-memory store that answers like the REST API.
- `test_woocommerce.py` covers rendering, the OAuth 1.0a signature, retries and waits,
  error messages, paging, the cursor, deletion and its safeguards, and two runs through a
  real `dlt` pipeline.
- `test_woocommerce_cognee.py` runs the source through `cognee.add()`, and through
  `cognify()` with the LLM mocked, to check that a deleted product leaves the graph.
