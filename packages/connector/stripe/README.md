# cognee-community-connector-stripe

A Stripe data-source connector for [cognee](https://github.com/topoteretes/cognee): sync a Stripe
account's customers, subscriptions, invoices, charges, products and disputes into memory, so you
can ask "which customers on the Pro plan have an open invoice?" or "why was Alphacorp's last
charge disputed?".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Each Stripe object
is rendered to text and ingested as a **document**. Related objects are linked in the graph:
a customer, its subscriptions, their invoices, the charges and the disputes share `NodeSet` nodes,
so they stay traversable. Later syncs read Stripe's events and re-read only what changed. Objects
deleted in Stripe are **forgotten** on the next sync.

Stripe I/O goes through the official `stripe` client, the library the
[dlt verified Stripe source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/stripe) is built
on, and uses that source's paging loop. Where cognee needs something different from the verified
source, see [Design notes](#design-notes).

## What gets ingested

| Object | Document contents | Linked to |
| --- | --- | --- |
| Customer | Name, description, created, currency, balance, delinquency | itself |
| Subscription | Status, dates (start, trial, cancellation), collection method, items with product, price, quantity and period end | customer, products |
| Invoice | Number, status, billing reason, period, due and paid dates, totals, the invoice's own customer-name snapshot, up to 20 line items | customer, subscription, products, the payment that paid it |
| Charge | Amount, status, captured and refunded amounts, failure reason, statement descriptor | customer, the payment it belongs to (shared with its invoice) |
| Product | Name, description, active, default price, unit label | itself |
| Dispute | Amount, reason, status, evidence due date | charge |

**Never ingested:** card numbers or any card fields, payment methods and sources, billing details,
addresses and shipping, tax ids, receipt and hosted-invoice links, risk scores and dispute
evidence. Each document is built from an allowlist of fields, never from the raw object, so a
field Stripe adds later cannot slip in. Customer email and phone (and the invoice's customer email)
are left out unless you pass `include_contact_details=True`. Free-form `metadata` is left out
unless you pass `include_metadata=True`.

Links are node-set memberships, recorded in the `cognee_node_set` column: a subscription row
carries `customer:cus_…`, `subscription:sub_…` and `product:prod_…`, and cognee stores them as
`stripe:customer:cus_…` and so on. Each document's chunks get a `belongs_to_set` edge to those
shared `NodeSet` nodes, so `customer → subscription → invoice → charge → dispute` is a path in the
graph. It is deterministic and does not depend on the LLM. Since API version 2025-03-31 a charge
no longer names its invoice, so the connector expands the invoice's `payments` and links the two
through the PaymentIntent that paid it (`payment_intent:pi_…`). Node sets also filter search
(`node_name=["stripe:customer:cus_…"]`).

## Requirements

Requires cognee 1.6.4: document mode with per-row node sets for the source, and the
`OAuthIntegration` resource picker and `run_sync` for the optional Stripe Connect integration.

## Install

```bash
uv pip install cognee-community-connector-stripe
# or, from this monorepo:
cd packages/connector/stripe && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_stripe import stripe_source

# The key and account fall back to STRIPE_API_KEY and STRIPE_ACCOUNT_ID.
await cognee.remember(
    stripe_source(platform_secret_key, account_id="acct_..."),
    dataset_name="stripe",
    primary_key="id",
    write_disposition="merge",  # required
)

answer = await cognee.search(
    query_text="Which customers have unpaid invoices?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["stripe"],
)
```

**`write_disposition="merge"` is required.** Incremental runs emit only the objects that changed
plus deletion tombstones. With `replace` they would empty everything else, so the source refuses
any other disposition before reading anything.

| Argument | Effect |
| --- | --- |
| `objects=[...]` | Which types to sync: `customers`, `subscriptions`, `invoices`, `charges`, `products`, `disputes` (default all). An empty list is valid and forgets everything synced before. |
| `created_since=...` | Keep only invoices, charges and disputes created on or after this datetime, date, ISO string or unix time. Older ones are forgotten. Customers, subscriptions and products are always synced whole. |
| `include_contact_details=True` | Also ingest customer email and phone. |
| `include_metadata=True` | Also ingest each object's `metadata`. |
| `full_refresh=True` | List everything this run instead of reading events. |

Use one Stripe account per dataset. See `examples/example.py` for the full flow.

## Setup: Stripe Connect

Users connect with **Stripe Connect OAuth** and the `read_only` scope; nobody pastes a secret
key. Following Stripe's current guidance, the connector reads a connected account with your
**platform's** secret key and the `Stripe-Account` header. It does not use the deprecated
per-account access token, so nothing secret is stored per connection.

1. In the Stripe Dashboard, open **Settings → Connect → Onboarding options → OAuth** and enable
   OAuth for Standard accounts.
2. Add your redirect URI (for the cognee integration below:
   `https://<your cognee host>/api/v1/integrations/stripe/callback`).
3. Copy the **client ID** (`ca_…`, test and live are separate) and your platform's **secret key**
   (`sk_…`, same mode).
4. Stripe grants `read_only` only to platforms set up as
   [extensions](https://docs.stripe.com/connect/oauth-changes-for-standard-platforms). If Stripe
   returns any other scope, the integration refuses the connection rather than keeping broader
   access than the user agreed to.

To try the connector on **your own** account without Connect, create a
[restricted key](https://dashboard.stripe.com/apikeys) with **Read** access to Customers,
Subscriptions, Invoices, Charges, Products, Disputes and Events, and leave the account id unset:

```bash
export STRIPE_API_KEY="rk_test_..."
```

## Connect from cognee (OAuth + object-type picker)

The package also ships `StripeIntegration`, an implementation of cognee's `OAuthIntegration`
contract. Once it is registered with the cognee API, cognee's generic routes serve the whole
connect flow:

| Route | What it does |
| --- | --- |
| `POST /api/v1/integrations/stripe/authorize` | Returns the Stripe Connect consent URL (`scope=read_only`) |
| `GET /api/v1/integrations/stripe/callback` | Exchanges the code and stores the connected account |
| `GET /api/v1/integrations/stripe/resources` | Lists the object types to choose from |
| `PUT /api/v1/integrations/stripe/resources` | Saves the selection (`{"resource_ids": ["customers", ...]}`) |
| `GET /api/v1/integrations/stripe/connection` | Shows the connection and its last sync status |
| `POST /api/v1/integrations/stripe/sync` | Syncs the selection into the account's own dataset |
| `DELETE /api/v1/integrations/stripe/connection` | Disconnects and deauthorizes the account at Stripe |

1. Register it in the process that serves cognee's API:

   ```python
   import cognee_community_connector_stripe.register  # noqa: F401
   from cognee.api.client import app  # serve this app, e.g. with uvicorn
   ```

2. Configure it (plus cognee's own `INTEGRATION_CREDENTIALS_KEY`):

   ```bash
   export STRIPE_CLIENT_ID="ca_..."
   export STRIPE_CLIENT_SECRET="sk_..."  # the platform's secret key
   export STRIPE_REDIRECT_URI="https://<your cognee host>/api/v1/integrations/stripe/callback"
   export STRIPE_FRONTEND_BASE_URL="https://<your cognee frontend>"
   export STRIPE_STATE_SECRET="<random string that signs the OAuth state>"
   export STRIPE_CREATED_SINCE="2025-01-01"  # optional history window
   ```

A new connection starts with nothing selected. The selection follows the picker's three states:
`null` syncs every type, `[]` syncs nothing (and forgets what was synced), and a list syncs those
types.

## How sync and forget-on-delete work

- **First run: full listing.** Every selected type is listed (100 objects per request,
  subscriptions with `status=all` so canceled ones are kept), and each object becomes a row.
- **Later runs: events.** Stripe's list endpoints filter on `created` only, so edits to old
  objects, like paying an invoice or renaming a customer, are invisible to a created-date
  cursor. Each run instead reads the events since the previous run started (with five minutes of
  overlap for clock skew), re-reads each object they name, and upserts it. A run with no changes
  costs one request. Events about cards and payment methods are dropped without being read.
- **Full listing again** when the selection or options change, when `full_refresh=True`, or when
  the cursor is older than Stripe's 30-day event retention. A type with more than 200 changes in
  one run is also listed rather than re-read one object at a time.
- **Forget-on-delete.** A deleted customer, product or draft invoice, an object that leaves the
  `created_since` window, and every object of a deselected type are emitted as `_deleted`
  tombstones. dlt drops the row on merge, and cognee's `orphan_cleanup` removes the document from
  the graph and vector stores. A full listing also tombstones every known id it did not see.
  Canceled subscriptions stay, with their new status.
- **Incremental in memory too.** Rendering is deterministic, using dates only, so a re-read
  object that did not really change keeps its content-hash `data_id` and is not re-cognified.
- **Failure safety.** Errors are never swallowed: an auth or permission error, retries exhausted
  on a `5xx`, or a network failure. The run aborts before dlt commits the rows and the cursor, so
  nothing is forgotten and the next run starts over. Only `404` and Stripe's `deleted: true` stub
  are treated as "gone".

## Design notes

**Document path, not the relational path.** Stripe objects are structured, so the relational dlt
path looks like the natural fit, and it was the first plan. But cognee builds row-to-row edges on
that path only from foreign keys declared in the dlt staging database, and dlt's destinations
never create foreign keys, not even from a `references` hint. On the relational path,
customer → subscription → invoice would therefore not be connected. The document path with
per-row node sets gives those edges deterministically, and also gives each object prose that
cognify can extract entities from.

**Where this differs from the dlt verified source**, and why:

| Verified source | This connector | Why |
| --- | --- | --- |
| Sets `stripe.api_key` globally | One `StripeClient` per source | Several connected accounts can sync in one API process |
| Secret key from `dlt.secrets` | Platform key + `Stripe-Account` (Connect) | The issue asks for Connect, and Stripe deprecates per-account tokens |
| Yields whole objects (`to_dict()`) | Allowlisted rendering | Charges carry `payment_method_details`, and invoices carry addresses and hosted links |
| `replace` windowed by `created`, or `append` for immutable types | `merge` driven by events, with tombstones | `created` misses edits, and deletions need a feed |
| API version `2022-11-15` | `2025-03-31.basil` | Current object shapes; both invoice shapes are handled |

**Limitations.** Product renames reach a subscription's document only through its link, not its
text: documents render ids for related objects, so one rename does not re-render thousands of
documents. Prices are rendered inside subscriptions and invoices, not synced on their own. An
invoice lists at most 20 line items. Only one Stripe account per dataset.

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests/
```

The tests need no Stripe account. Stripe is served by an in-memory fake mounted as a `requests`
transport adapter behind the official client, so the real headers, paging and error mapping run.
They cover:

- rendering of every object type, amounts in zero- and three-decimal currencies, and node sets
  across both invoice shapes
- a sweep that fills every card, billing, address and link field with a marker and checks it
  never reaches a row, plus the opt-in contact details and metadata
- the full listing (paging, `status=all`, the `Stripe-Account` and pinned version headers) and
  the `created_since` window
- the events cursor: an unchanged account costs one request, edits and new objects are re-read,
  clock skew is covered, card events are never fetched, and bulk changes fall back to a listing
- deletes of customers, products and draft invoices, canceled subscriptions, a stale cursor
  after event retention, deselection and the empty selection
- failure safety, and a real `dlt` merge pipeline where tombstones drop rows, the cursor survives
  between runs, a failed run commits nothing, and a `replace` run is refused
- the Connect integration: read-only authorize URL, Basic-auth code exchange, refusal of a broader
  scope, no tokens stored, the three-state picker and deauthorize on disconnect
- an end-to-end `cognee.add` + `cognify` run (LLM mocked) where node sets link customer →
  subscription → invoice → charge → dispute in the graph, card digits never reach it, unchanged
  documents keep their ids, and a deleted customer leaves the graph
- the integration syncing a stored selection end to end, and a smoke run of `examples/example.py`
