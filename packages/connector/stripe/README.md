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
| Invoice | Number, status, billing reason, period, due and paid dates, totals, the invoice's own customer-name snapshot, line items | customer, subscription, products, the payment that paid it |
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
`stripe:customer:cus_…` and so on. Only records that something else refers to get a node set of
their own (customers, subscriptions, products and charges), so an invoice or a dispute adds none.
Each document's chunks get a `belongs_to_set` edge to those shared `NodeSet` nodes, so
`customer → subscription → invoice → charge → dispute` is a path in the graph. It is
deterministic and does not depend on the LLM. Since API version 2025-03-31 a charge
no longer names its invoice, so the connector expands the invoice's `payments` and links the two
through the PaymentIntent that paid it (`payment_intent:pi_…`). Node sets also filter search
(`node_name=["stripe:customer:cus_…"]`).

## Requirements

Python 3.10 to 3.14 and cognee 1.6.4 or later: document mode with per-row node sets for the
source (checked when a source is built, so an older cognee fails with a clear error), and the
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
| `max_requests=5000` | Stripe requests one run may make (`None` lifts the limit). A run that spends them, or hits a rate limit that outlasts the client's retries, stops cleanly and resumes next run. |
| `resource_name=...` | Names the dlt resource, the pipeline scope and the staging tables. Defaults to a name derived from `account_id`, so accounts never share a cursor. |
| `check_active=...` | Called before every request and row; raising stops the sync (used when a connection can be revoked mid-run). |

The credentials are arguments, not environment lookups; `examples/example.py` shows reading them
from `STRIPE_API_KEY` and `STRIPE_ACCOUNT_ID`. Several Stripe accounts can sync into one dataset:
each gets its own resource, cursor and tables. See `examples/example.py` for the full flow.

## Try it on a free test account

Stripe's test mode is free and needs no business verification or Connect.

1. In the Dashboard, switch to **test mode** and copy your secret test key (`sk_test_…`) from
   Developers → API keys.
2. Seed some billing history: a paid subscription, a disputed payment and an open invoice. Run
   it once from `packages/connector/stripe`; it stops if the account is already seeded.

   ```bash
   export STRIPE_SEED_KEY="sk_test_..."
   uv run python examples/seed_test_data.py
   ```

3. Create a **restricted key** (Developers → API keys → Create restricted key) with **Read**
   access to customers, subscriptions, invoices, charges, products, disputes and events, and
   nothing else. The connector needs no write access.
4. Sync and search with that key, and leave `STRIPE_ACCOUNT_ID` unset to read your own account:

   ```bash
   export STRIPE_API_KEY="rk_test_..."
   export LLM_API_KEY="sk-..."
   uv run python examples/example.py
   ```

Then rename a customer, delete another or void an invoice in the Dashboard and run it again: the
change is picked up from Stripe's events and the deleted customer leaves the graph.

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
4. The integration asks for `read_only`. Stripe's docs list that scope for
   [extensions](https://docs.stripe.com/connect/oauth-changes-for-standard-platforms), so check
   how your platform is registered. If Stripe returns any other scope, the integration refuses
   the connection rather than keeping broader access than the user agreed to: the callback ends
   in `error_exchange_failed` and the server log names the scope Stripe returned and says the
   platform must be an extension. Register the platform as an extension (Stripe's docs say to
   contact Stripe about that) to connect read-only.

To try the connector without Connect, see [Try it on a free test account](#try-it-on-a-free-test-account).

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
  overlap for clock skew), re-reads each object they name, and upserts it. Stripe filters the
  events on the server by family (`customer.*`, `invoice.*`, `charge.*`, `product.*`, only those of
  the selected types), so an unchanged account costs one request per family and events about cards
  and payment methods are never sent.
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
- **A run that cannot finish.** Every request counts against `max_requests`. When the budget is
  spent, or a rate limit outlasts the client's retries, the run stops cleanly: the rows it read
  are kept, a full listing remembers where it stopped, and the next run resumes there, so an
  account too big for one run still converges. Nothing is forgotten from a partial listing; ids
  are tombstoned only once their type has been listed to the end. If reading the events alone
  would take half the budget, the run lists instead. The sync counts carry `failed` and the
  reason (`failed_budget`, `failed_rate_limit`, `failed_auth`).
- **Failure safety.** An auth or permission error, retries exhausted on a `5xx`, or a network
  failure aborts the run before dlt commits the rows and the cursor, so nothing is forgotten and
  the next run starts over. Only `404` and Stripe's `deleted: true` stub are treated as "gone".
  The API key is not echoed in the errors or logs of a 401, 403, 404, 429 or 5xx.

## Design notes

**Document path, not the relational path.** Stripe objects are structured, so the relational dlt
path looks like the natural fit, and it was the first plan. But cognee builds row-to-row edges on
that path only from foreign keys declared in the dlt staging database. dlt's SQLite staging tables
carry none, even when the resource declares `references` (checked with dlt 1.30), so on the
relational path customer → subscription → invoice would not be connected. The document path with
per-row node sets gives those edges deterministically, and also gives each object prose that
cognify can extract entities from.

**Where this differs from the dlt verified source**, and why:

| Verified source | This connector | Why |
| --- | --- | --- |
| Sets `stripe.api_key` globally | One `StripeClient` per source | Each source carries its own credentials |
| Secret key from `dlt.secrets` | Key passed by the caller, with `Stripe-Account` for Connect | The caller decides where credentials come from, and Stripe deprecates per-account tokens |
| Yields whole objects (`to_dict()`) | Allowlisted rendering | Charges carry `payment_method_details`, and invoices carry addresses and hosted links |
| `replace` windowed by `created`, or `append` for immutable types | `merge` driven by events, with tombstones | `created` misses edits, and deletions need a feed |
| API version `2022-11-15` | `2025-03-31.basil` | Current object shapes, pinned so every account returns the same ones |

**Limitations.**

- Product renames reach a subscription's document only through its link, not its text: documents
  render ids for related objects, so one rename does not re-render thousands of documents.
- Prices are rendered inside subscriptions and invoices, not synced on their own.
- An invoice shows the line items Stripe returns inline (the first 10) and links the first 10
  payment attempts.
- The sync state keeps the id of every synced object (a few dozen bytes each) to detect
  deletions, so it grows with the account.
- Every record that something refers to has a node set of its own, so a very large account has
  many `NodeSet` nodes and `node_name` filters narrow to one record. These per-record sets are
  meant to be replaced by cognee's `cognee_structure` column once a stable cognee includes it (it
  is in 1.6.6rc1). Node sets are part of a row's identity, so switching will re-ingest every
  account synced before it.

## Testing

```bash
uv run --with pytest --with pytest-asyncio pytest tests/
```

The tests need no Stripe account. Stripe is served by an in-memory fake mounted as a `requests`
transport adapter behind the official client, so the real headers, paging and error mapping run.
The fake follows the response shapes of a real test account: a charge carries no `invoice` field,
and an invoice's `payments` come back only when the request expands them. The tests cover:

- rendering of every object type, amounts in zero- and three-decimal currencies, and node sets
  including the PaymentIntent that ties an invoice to its charge
- a sweep that fills every card, billing, address and link field with a marker and checks it
  never reaches a row, plus the opt-in contact details and metadata
- the full listing (paging, `status=all`, the `Stripe-Account` and pinned version headers) and
  the `created_since` window
- the events cursor: an unchanged account costs one request, edits and new objects are re-read,
  clock skew is covered, card events are never fetched, and bulk changes fall back to a listing
- deletes of customers, products and draft invoices, canceled subscriptions, a stale cursor
  after event retention, deselection and the empty selection
- events filtered on the server: unrelated events never reach the client, and only the families
  of the selected types are asked for
- a spent budget and a rate limit stopping a run cleanly, the next run resuming where it stopped,
  a partial listing forgetting nothing, a long event walk falling back to a listing, and the
  connection being checked before every request
- failure safety, and a real `dlt` merge pipeline where tombstones drop rows, the cursor survives
  between runs, a failed run commits nothing, and a `replace` run is refused
- the API key never showing up in the exception text or the logs of a 401, 403, 404, 429 or 5xx
- two accounts in one dataset keeping their own tables and cursors, and an older cognee being
  refused with a clear error
- the Connect integration: read-only authorize URL, Basic-auth code exchange, refusal of a broader
  scope, no tokens stored, the three-state picker and deauthorize on disconnect
- an end-to-end `cognee.add` + `cognify` run (LLM mocked) where node sets link customer →
  subscription → invoice → charge → dispute in the graph, card digits never reach it, unchanged
  documents keep their ids, a deleted customer leaves the graph, and two accounts syncing into one
  dataset do not forget each other's documents
- the integration syncing a stored selection end to end, and a smoke run of `examples/example.py`
