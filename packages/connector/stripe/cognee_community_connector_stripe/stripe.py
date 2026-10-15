"""DLT source for Stripe billing data (events cursor + forget-on-delete).

Reads the customers, subscriptions, invoices, charges, products and disputes
of one Stripe account and yields each object as a document for cognee's
ingestion pipeline::

    import cognee
    from cognee_community_connector_stripe import stripe_source

    await cognee.remember(
        stripe_source(platform_secret_key, account_id="acct_..."),
        dataset_name="stripe",
        primary_key="id",
        write_disposition="merge",  # required: the source emits deletions
    )

Design
------
* **Built on the dlt verified source**: Stripe I/O uses the official
  ``stripe`` client and the verified source's paging loop (``limit=100`` +
  ``starting_after``, ``status=all`` for subscriptions). It differs where
  cognee needs it to: requests carry the key per client instead of setting
  ``stripe.api_key`` globally, so two connected accounts can sync in one
  process; objects are rendered through allowlists instead of being yielded
  whole; and lists are not windowed by ``created`` alone, which misses edits.
* **Stripe Connect**: requests use the platform's secret key with the
  ``Stripe-Account`` header of the connected account, as Stripe recommends
  over the deprecated per-account OAuth access token.
* **Documents, linked by node sets**: one row per object in
  ``stripe_<object>`` tables (the id is Stripe's own, ``cus_...``,
  ``sub_...``). The source declares ``cognee_document_source = "stripe"``. A
  relational dlt source would get no row-to-row edges, because dlt creates no
  foreign keys in the staging database that cognee reads them from. Instead
  every row names its related objects in ``cognee_node_set``
  (``customer:cus_1``, ``subscription:sub_1``, ...), so a customer, its
  subscriptions, their invoices, the charges and the disputes are joined
  through shared ``NodeSet`` nodes in the graph.
* **Never card data**: rows are built from per-object allowlists. Payment
  methods, card and billing details, addresses, receipt and hosted-invoice
  links and dispute evidence are never read into a row, so a field Stripe
  adds later cannot leak in. Contact details (email, phone) and free-form
  ``metadata`` are opt-in.
* **Incremental sync**: Stripe list endpoints filter on ``created`` only, so
  edits to old objects are found through the Events API instead. A run lists
  the events since the last run's start, re-reads each changed object and
  upserts it (``write_disposition="merge"``). The first run, a changed
  selection, or a cursor older than Stripe's 30-day event retention does a
  full listing instead.
* **Forget-on-delete**: an object that is deleted upstream (a deleted
  customer, product or draft invoice) or leaves the selection is emitted as a
  ``_deleted`` tombstone, so dlt drops the row and cognee's orphan cleanup
  removes it from the graph and vector stores. A full listing tombstones every
  known id that was not listed.
* **Failure safety**: errors (auth, permissions, exhausted retries, network)
  are not swallowed. They abort the run before dlt commits the rows and the
  cursor, so nothing is forgotten and the next run starts over.
"""

import os
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import dlt
import stripe
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    NODE_SET_COLUMN_HINT,
    PIPELINE_SCOPE_ATTR,
    guarded_rows,
)

logger = get_logger("stripe_connector")

STRIPE_SOURCE_NAME = "stripe"
STRIPE_RESOURCE_NAME = "stripe_objects"

API_KEY_ENV = "STRIPE_API_KEY"
ACCOUNT_ID_ENV = "STRIPE_ACCOUNT_ID"

# Pinned, so every connected account returns the same object shapes whatever
# its own default API version is.
STRIPE_API_VERSION = "2025-03-31.basil"

_PAGE_SIZE = 100
_MAX_NETWORK_RETRIES = 3

# Stripe keeps events for 30 days. A cursor older than this (with a day of
# margin) may have missed changes, so the run falls back to a full listing.
_EVENT_RETENTION_SECONDS = 29 * 24 * 3600
# Events are read from a little before the cursor, so clock skew between this
# host and Stripe cannot drop an event. Re-reading an object is harmless.
_CURSOR_OVERLAP_SECONDS = 300
# Past this many changed objects of one type, listing the type (100 per
# request) is cheaper than retrieving each one.
_MAX_RETRIEVES_PER_TYPE = 200

# Bump when rendering or the state layout changes, so the next run re-renders
# every object instead of keeping rows rendered by an older connector.
_STATE_VERSION = 1

# Currencies whose amounts Stripe does not express in hundredths.
_ZERO_DECIMAL_CURRENCIES = {
    "bif",
    "clp",
    "djf",
    "gnf",
    "jpy",
    "kmf",
    "krw",
    "mga",
    "pyg",
    "rwf",
    "ugx",
    "vnd",
    "vuv",
    "xaf",
    "xof",
    "xpf",
}
_THREE_DECIMAL_CURRENCIES = {"bhd", "jod", "kwd", "omr", "tnd"}

# Invoices list at most this many of their line items inline.
_MAX_INVOICE_LINES = 20


@dataclass(frozen=True)
class RenderOptions:
    """What a row may contain beyond the always-safe allowlist."""

    include_contact_details: bool = False
    include_metadata: bool = False


@dataclass(frozen=True)
class _ObjectType:
    name: str
    # The ``object`` field Stripe sets on these objects (and on event payloads).
    stripe_object: str
    dashboard_path: str
    # History objects honour ``created_since``; the others are always whole.
    windowed: bool
    list_params: dict[str, Any]
    render: Callable[[dict, "RenderOptions"], dict[str, Any]]

    @property
    def table_name(self) -> str:
        return f"stripe_{self.name}"


def stripe_client(
    api_key: str | None = None,
    *,
    account_id: str | None = None,
    http_client: Any = None,
) -> stripe.StripeClient:
    """Return a ``StripeClient`` scoped to one account.

    Args:
        api_key: The Connect platform's secret key, or, to try the connector
            on your own account, a restricted key with read access. Falls back
            to ``STRIPE_API_KEY``.
        account_id: The connected account (``acct_...``) sent as
            ``Stripe-Account``. Falls back to ``STRIPE_ACCOUNT_ID``; omit it
            to read the key's own account.
        http_client: A ``stripe`` HTTP client (mainly a test-injection point).
    """
    api_key = api_key or os.environ.get(API_KEY_ENV)
    if not api_key:
        raise ValueError(f"Stripe API key required: pass api_key= or set {API_KEY_ENV}.")
    return stripe.StripeClient(
        api_key,
        stripe_account=account_id or os.environ.get(ACCOUNT_ID_ENV) or None,
        stripe_version=STRIPE_API_VERSION,
        max_network_retries=_MAX_NETWORK_RETRIES,
        http_client=http_client,
    )


def stripe_source(
    api_key: str | None = None,
    *,
    account_id: str | None = None,
    objects: Sequence[str] | None = None,
    created_since: datetime | date | str | int | None = None,
    include_contact_details: bool = False,
    include_metadata: bool = False,
    full_refresh: bool = False,
    check_active: Callable[[], None] | None = None,
    http_client: Any = None,
):
    """Create a dlt source that yields Stripe objects as documents.

    Args:
        api_key: The Connect platform's secret key (or a restricted read key of
            your own account). Falls back to ``STRIPE_API_KEY``.
        account_id: The connected account to read (``acct_...``). Falls back
            to ``STRIPE_ACCOUNT_ID``.
        objects: What to ingest, any of ``customers``, ``subscriptions``,
            ``invoices``, ``charges``, ``products`` and ``disputes`` (default:
            all). An empty list is valid and forgets everything synced before.
        created_since: Only keep invoices, charges and disputes created at or
            after this time (a datetime, date, ISO string or unix timestamp).
            Older ones are forgotten. Customers, subscriptions and products are
            always synced whole.
        include_contact_details: Also ingest customer email and phone.
        include_metadata: Also ingest each object's free-form ``metadata``.
        full_refresh: List everything this run instead of reading events.
        check_active: Called between rows; raises to stop a sync whose
            connection was revoked mid-run (used by the cognee integration).
        http_client: A ``stripe`` HTTP client (mainly a test-injection point).

    Returns:
        A dlt source for ``cognee.remember(...)``. Pass
        ``write_disposition="merge"``: incremental runs emit only changes and
        tombstones, and any other disposition is refused.
    """
    selected = list(dict.fromkeys(OBJECT_TYPES if objects is None else objects))
    unknown = [name for name in selected if name not in OBJECT_TYPES]
    if unknown:
        raise ValueError(
            f"Unknown Stripe object type(s) {unknown}; choose from {sorted(OBJECT_TYPES)}."
        )
    client = stripe_client(api_key, account_id=account_id, http_client=http_client)
    options = RenderOptions(
        include_contact_details=include_contact_details, include_metadata=include_metadata
    )
    since = _unix_time(created_since)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=STRIPE_RESOURCE_NAME,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the staging table on merge,
        # which is what makes cognee's orphan cleanup forget them.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}, **NODE_SET_COLUMN_HINT},
    )
    def stripe_objects():
        # The caller's write_disposition overrides the one declared above. A
        # replace run of an event delta would empty every table but the
        # changed rows, so refuse it before reading anything.
        disposition = source.resources[STRIPE_RESOURCE_NAME].write_disposition
        if disposition != "merge":
            raise ValueError(
                f'stripe_source needs write_disposition="merge", got {disposition!r}. '
                "Its incremental runs emit only changes and deletions."
            )
        rows = sync_objects(
            client,
            selected,
            dlt.current.resource_state(),
            created_since=since,
            options=options,
            full_refresh=full_refresh,
            stats=stats,
        )
        for table_name, row in guarded_rows(rows, check_active):
            yield dlt.mark.with_table_name(row, table_name)

    @dlt.source(name=STRIPE_SOURCE_NAME)
    def _stripe():
        return stripe_objects

    source = _stripe()
    # Opt into the document path (object → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, DOCUMENT_SOURCE_ATTR, STRIPE_SOURCE_NAME)
    # Give the source its own dlt pipeline per dataset, so its cursor is not
    # shared with unrelated dlt sources.
    setattr(source, PIPELINE_SCOPE_ATTR, STRIPE_SOURCE_NAME)
    # Counts of the last run (objects written, objects forgotten), for hosts
    # that record how a sync went.
    source.cognee_sync_stats = stats
    return source


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------


def sync_objects(
    client: stripe.StripeClient,
    objects: Sequence[str],
    state: dict,
    *,
    created_since: int | None = None,
    options: RenderOptions | None = None,
    full_refresh: bool = False,
    now: float | None = None,
    stats: dict[str, int] | None = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(table_name, row)`` for everything that changed since the last run.

    ``state`` holds the events ``cursor`` (when the last good run started),
    the ``scope`` it covered and the ``known`` ids per object type. dlt only
    persists it when the whole run loads, so a failed run leaves it as it
    was and the next run repeats the work. ``stats``, when given, is filled
    with the number of objects ``written`` and ``deleted``.
    """
    options = options or RenderOptions()
    started = int(time.time() if now is None else now)
    scope = {
        "version": _STATE_VERSION,
        "objects": sorted(objects),
        "created_since": created_since,
        "contact": options.include_contact_details,
        "metadata": options.include_metadata,
    }
    previous_known: dict[str, list[str]] = state.get("known") or {}
    cursor = state.get("cursor")
    incremental = (
        not full_refresh
        and state.get("scope") == scope
        and isinstance(cursor, int)
        and started - cursor < _EVENT_RETENTION_SECONDS
    )

    known: dict[str, set[str]] = {}
    stats = {} if stats is None else stats
    stats.clear()
    stats.update(written=0, deleted=0)
    changed = _changed_ids(client, cursor - _CURSOR_OVERLAP_SECONDS) if incremental else {}

    for name in objects:
        object_type = OBJECT_TYPES[name]
        old_ids = set(previous_known.get(name) or [])
        ids = changed.get(object_type.stripe_object, set())
        if incremental and len(ids) <= _MAX_RETRIEVES_PER_TYPE:
            rows = _sync_changed(client, object_type, ids, old_ids, created_since, options)
        else:
            rows = _sync_all(client, object_type, old_ids, created_since, options)
        known[name] = set(old_ids)
        for row in rows:
            if row["_deleted"]:
                known[name].discard(row["id"])
                stats["deleted"] += 1
            else:
                known[name].add(row["id"])
                stats["written"] += 1
            yield object_type.table_name, row

    # Types that left the selection are forgotten.
    for name, ids in previous_known.items():
        if name in known or name not in OBJECT_TYPES:
            continue
        for object_id in ids:
            stats["deleted"] += 1
            yield OBJECT_TYPES[name].table_name, _tombstone(object_id)

    state["cursor"] = started
    state["scope"] = scope
    state["known"] = {name: sorted(ids) for name, ids in known.items()}
    logger.info(
        "Stripe: %s sync of %s: %d object(s) written, %d forgotten.",
        "incremental" if incremental else "full",
        ", ".join(objects) or "nothing",
        stats["written"],
        stats["deleted"],
    )


def _sync_all(
    client: stripe.StripeClient,
    object_type: _ObjectType,
    old_ids: set[str],
    created_since: int | None,
    options: RenderOptions,
) -> Iterator[dict[str, Any]]:
    """List every object of a type; tombstone known ids that were not listed."""
    params = dict(object_type.list_params)
    if object_type.windowed and created_since is not None:
        params["created"] = {"gte": created_since}
    listed: set[str] = set()
    for obj in list_all(_service(client, object_type), params):
        listed.add(obj["id"])
        yield _row(object_type, obj, options)
    for object_id in sorted(old_ids - listed):
        yield _tombstone(object_id)


def _sync_changed(
    client: stripe.StripeClient,
    object_type: _ObjectType,
    ids: set[str],
    old_ids: set[str],
    created_since: int | None,
    options: RenderOptions,
) -> Iterator[dict[str, Any]]:
    """Re-read the objects events named; tombstone the ones that are gone."""
    service = _service(client, object_type)
    for object_id in sorted(ids):
        obj = _retrieve(service, object_id)
        out_of_window = (
            obj is not None
            and object_type.windowed
            and created_since is not None
            and (obj.get("created") or 0) < created_since
        )
        if obj is None or out_of_window:
            if object_id in old_ids:
                yield _tombstone(object_id)
            continue
        yield _row(object_type, obj, options)


def _changed_ids(client: stripe.StripeClient, since: int) -> dict[str, set[str]]:
    """Ids of the objects named by events since ``since``, keyed by object type.

    Every event is read rather than filtered by type: the ``types`` filter
    takes no wildcards and at most 20 names, fewer than the invoice events
    alone. Events about objects this connector never reads (cards, payment
    methods) are dropped here, unread.
    """
    changed: dict[str, set[str]] = {}
    for event in list_all(client.v1.events, {"created": {"gte": since}}):
        obj = (event.get("data") or {}).get("object") or {}
        # An upcoming invoice has no id yet; there is nothing to re-read.
        if obj.get("id") and obj.get("object") in _STRIPE_OBJECTS:
            changed.setdefault(obj["object"], set()).add(obj["id"])
    return changed


def list_all(service: Any, params: dict[str, Any]) -> Iterator[dict]:
    """Yield every object of a Stripe list endpoint as a plain dict.

    The dlt verified source's paging loop: pages of 100, each one starting
    after the last id of the page before.
    """
    starting_after = None
    while True:
        page_params = {**params, "limit": _PAGE_SIZE}
        if starting_after:
            page_params["starting_after"] = starting_after
        page = service.list(params=page_params)
        data = [obj.to_dict() for obj in page.data]
        yield from data
        if not page.has_more or not data:
            return
        starting_after = data[-1]["id"]


def _retrieve(service: Any, object_id: str) -> dict | None:
    """The object as a dict, or None when it was deleted."""
    try:
        obj = service.retrieve(object_id).to_dict()
    except stripe.InvalidRequestError as error:
        if error.http_status == 404:
            return None
        raise
    # Deleted customers stay retrievable as a stub that says so.
    return None if obj.get("deleted") else obj


def _service(client: stripe.StripeClient, object_type: _ObjectType) -> Any:
    return getattr(client.v1, object_type.name)


def _row(object_type: _ObjectType, obj: dict, options: RenderOptions) -> dict[str, Any]:
    row = object_type.render(obj, options)
    test_mode = "" if obj.get("livemode") else "/test"
    return {
        "id": obj["id"],
        "title": row["title"],
        "content": row["content"],
        "url": f"https://dashboard.stripe.com{test_mode}/{object_type.dashboard_path}/{obj['id']}",
        NODE_SET_COLUMN: sorted({node for node in row["node_sets"] if node}),
        "_deleted": False,
    }


def _tombstone(object_id: str) -> dict[str, Any]:
    return {"id": object_id, "_deleted": True}


# ---------------------------------------------------------------------------
# Rendering: allowlists only. Never add a field that holds card, bank or
# billing details, addresses, receipt/hosted links or dispute evidence.
# ---------------------------------------------------------------------------


def render_customer(customer: dict, options: RenderOptions) -> dict[str, Any]:
    name = customer.get("name") or customer["id"]
    lines = [
        f"Stripe customer {name} ({customer['id']}).",
        _field("Description", customer.get("description")),
        _field("Created", _day(customer.get("created"))),
        _field("Currency", (customer.get("currency") or "").upper()),
        _field("Balance", _money(customer.get("balance") or None, customer.get("currency"))),
        "Delinquent: yes" if customer.get("delinquent") else "",
    ]
    if options.include_contact_details:
        lines += [_field("Email", customer.get("email")), _field("Phone", customer.get("phone"))]
    lines += _metadata(customer, options)
    return {
        "title": f"Customer: {name}",
        "content": _join(lines),
        "node_sets": [_node("customer", customer["id"])],
    }


def render_subscription(subscription: dict, options: RenderOptions) -> dict[str, Any]:
    customer_id = _id(subscription.get("customer"))
    items = (subscription.get("items") or {}).get("data") or []
    lines = [
        f"Stripe subscription {subscription['id']} of customer {customer_id}.",
        _field("Status", subscription.get("status")),
        _field("Description", subscription.get("description")),
        _field("Created", _day(subscription.get("created"))),
        _field("Started", _day(subscription.get("start_date"))),
        _field("Trial ends", _day(subscription.get("trial_end"))),
        _field("Collection", subscription.get("collection_method")),
        _field("Cancels at", _day(subscription.get("cancel_at"))),
        "Cancels at period end: yes" if subscription.get("cancel_at_period_end") else "",
        _field("Canceled", _day(subscription.get("canceled_at"))),
        _field("Ended", _day(subscription.get("ended_at"))),
    ]
    node_sets = [_node("customer", customer_id), _node("subscription", subscription["id"])]
    if items:
        lines.append("Items:")
    for item in items:
        price = item.get("price") or {}
        product_id = _id(price.get("product"))
        node_sets.append(_node("product", product_id))
        quantity = item.get("quantity")
        period_end = _day(item.get("current_period_end"))
        lines.append(
            "- "
            + _parts(
                f"product {product_id}" if product_id else "",
                price.get("nickname"),
                _price(price),
                f"quantity {quantity}" if quantity is not None else "",
                f"current period ends {period_end}" if period_end else "",
            )
        )
    lines += _metadata(subscription, options)
    return {
        "title": f"Subscription {subscription['id']} ({subscription.get('status') or 'unknown'})",
        "content": _join(lines),
        "node_sets": node_sets,
    }


def render_invoice(invoice: dict, options: RenderOptions) -> dict[str, Any]:
    customer_id = _id(invoice.get("customer"))
    # Before API version 2025-03-31 the subscription sat on the invoice itself.
    subscription_details = (invoice.get("parent") or {}).get("subscription_details") or {}
    subscription_id = _id(invoice.get("subscription")) or _id(
        subscription_details.get("subscription")
    )
    currency = invoice.get("currency")
    label = invoice.get("number") or invoice["id"]
    transitions = invoice.get("status_transitions") or {}
    lines = [
        f"Stripe invoice {label} ({invoice['id']}) for customer {customer_id}.",
        # The invoice's own snapshot of the name, taken when it was finalized.
        _field("Customer name", invoice.get("customer_name")),
        _field("Subscription", subscription_id),
        _field("Status", invoice.get("status")),
        _field("Billing reason", invoice.get("billing_reason")),
        _field("Description", invoice.get("description")),
        _field("Created", _day(invoice.get("created"))),
        _field("Period", _period(invoice.get("period_start"), invoice.get("period_end"))),
        _field("Due", _day(invoice.get("due_date"))),
        _field("Paid", _day(transitions.get("paid_at"))),
        _field("Voided", _day(transitions.get("voided_at"))),
        _field("Total", _money(invoice.get("total"), currency)),
        _field("Amount due", _money(invoice.get("amount_due"), currency)),
        _field("Amount paid", _money(invoice.get("amount_paid"), currency)),
        _field("Amount remaining", _money(invoice.get("amount_remaining"), currency)),
    ]
    if options.include_contact_details:
        lines.append(_field("Customer email", invoice.get("customer_email")))
    node_sets = [
        _node("customer", customer_id),
        _node("invoice", invoice["id"]),
        _node("subscription", subscription_id),
    ]
    line_items = invoice.get("lines") or {}
    entries = line_items.get("data") or []
    if entries:
        lines.append("Lines:")
    for entry in entries[:_MAX_INVOICE_LINES]:
        product_id = _line_product(entry)
        node_sets.append(_node("product", product_id))
        quantity = entry.get("quantity")
        lines.append(
            "- "
            + _parts(
                entry.get("description"),
                f"product {product_id}" if product_id else "",
                f"quantity {quantity}" if quantity is not None else "",
                _money(entry.get("amount"), entry.get("currency") or currency),
            )
        )
    if line_items.get("has_more") or len(entries) > _MAX_INVOICE_LINES:
        lines.append("- (more lines in Stripe)")
    lines += _metadata(invoice, options)
    return {
        "title": f"Invoice {label} ({invoice.get('status') or 'unknown'})",
        "content": _join(lines),
        "node_sets": node_sets,
    }


def render_charge(charge: dict, options: RenderOptions) -> dict[str, Any]:
    customer_id = _id(charge.get("customer"))
    invoice_id = _id(charge.get("invoice"))
    currency = charge.get("currency")
    amount = _money(charge.get("amount"), currency)
    of_customer = f" of customer {customer_id}" if customer_id else ""
    lines = [
        f"Stripe charge {charge['id']}{of_customer} for {amount}.",
        _field("Status", charge.get("status")),
        _field("Description", charge.get("description")),
        _field("Invoice", invoice_id),
        _field("Created", _day(charge.get("created"))),
        _field("Captured", _money(charge.get("amount_captured"), currency)),
        _field("Refunded", _money(charge.get("amount_refunded") or None, currency)),
        "Disputed: yes" if charge.get("disputed") else "",
        _field("Failure", charge.get("failure_message") or charge.get("failure_code")),
        _field("Statement descriptor", charge.get("calculated_statement_descriptor")),
    ]
    lines += _metadata(charge, options)
    return {
        "title": f"Charge {charge['id']} ({amount}, {charge.get('status') or 'unknown'})",
        "content": _join(lines),
        "node_sets": [
            _node("charge", charge["id"]),
            _node("customer", customer_id),
            _node("invoice", invoice_id),
        ],
    }


def render_product(product: dict, options: RenderOptions) -> dict[str, Any]:
    name = product.get("name") or product["id"]
    lines = [
        f"Stripe product {name} ({product['id']}).",
        _field("Description", product.get("description")),
        "Active: yes" if product.get("active") else "Active: no",
        _field("Default price", _id(product.get("default_price"))),
        _field("Unit label", product.get("unit_label")),
        _field("Created", _day(product.get("created"))),
    ]
    lines += _metadata(product, options)
    return {
        "title": f"Product: {name}",
        "content": _join(lines),
        "node_sets": [_node("product", product["id"])],
    }


def render_dispute(dispute: dict, options: RenderOptions) -> dict[str, Any]:
    charge_id = _id(dispute.get("charge"))
    amount = _money(dispute.get("amount"), dispute.get("currency"))
    lines = [
        f"Stripe dispute {dispute['id']} on charge {charge_id} for {amount}.",
        _field("Reason", dispute.get("reason")),
        _field("Status", dispute.get("status")),
        _field("Created", _day(dispute.get("created"))),
        _field("Evidence due", _day((dispute.get("evidence_details") or {}).get("due_by"))),
    ]
    lines += _metadata(dispute, options)
    return {
        "title": f"Dispute {dispute['id']} ({dispute.get('reason') or 'unknown reason'})",
        "content": _join(lines),
        "node_sets": [_node("dispute", dispute["id"]), _node("charge", charge_id)],
    }


OBJECT_TYPES: dict[str, _ObjectType] = {
    object_type.name: object_type
    for object_type in (
        _ObjectType(
            name="customers",
            stripe_object="customer",
            dashboard_path="customers",
            windowed=False,
            list_params={},
            render=render_customer,
        ),
        _ObjectType(
            name="subscriptions",
            stripe_object="subscription",
            dashboard_path="subscriptions",
            windowed=False,
            # Without status=all Stripe leaves canceled subscriptions out.
            list_params={"status": "all"},
            render=render_subscription,
        ),
        _ObjectType(
            name="invoices",
            stripe_object="invoice",
            dashboard_path="invoices",
            windowed=True,
            list_params={},
            render=render_invoice,
        ),
        _ObjectType(
            name="charges",
            stripe_object="charge",
            dashboard_path="payments",
            windowed=True,
            list_params={},
            render=render_charge,
        ),
        _ObjectType(
            name="products",
            stripe_object="product",
            dashboard_path="products",
            windowed=False,
            list_params={},
            render=render_product,
        ),
        _ObjectType(
            name="disputes",
            stripe_object="dispute",
            dashboard_path="disputes",
            windowed=True,
            list_params={},
            render=render_dispute,
        ),
    )
}
_STRIPE_OBJECTS = {object_type.stripe_object for object_type in OBJECT_TYPES.values()}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _node(kind: str, object_id: str | None) -> str:
    # cognee namespaces these under the source tag: "stripe:customer:cus_1".
    return f"{kind}:{object_id}" if object_id else ""


def _id(value: Any) -> str | None:
    """An object id, whether the field holds the id or the expanded object."""
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) and value else None


def _line_product(entry: dict) -> str | None:
    # Older API versions put the price on the line; newer ones under pricing.
    price_details = (entry.get("pricing") or {}).get("price_details") or {}
    return _id(price_details.get("product")) or _id((entry.get("price") or {}).get("product"))


def _field(label: str, value: Any) -> str:
    return f"{label}: {value}" if value not in (None, "") else ""


def _parts(*parts: Any) -> str:
    return ", ".join(str(part) for part in parts if part)


def _join(lines: list[str]) -> str:
    return "\n".join(line for line in lines if line)


def _metadata(obj: dict, options: RenderOptions) -> list[str]:
    metadata = obj.get("metadata") or {}
    if not options.include_metadata or not metadata:
        return []
    return ["Metadata:", *(f"- {key}: {metadata[key]}" for key in sorted(metadata))]


def _money(amount: Any, currency: str | None) -> str:
    """Render a Stripe amount (in the currency's smallest unit) as ``49.00 USD``."""
    if not isinstance(amount, int) or isinstance(amount, bool):
        return ""
    code = (currency or "").lower()
    if code in _ZERO_DECIMAL_CURRENCIES:
        value = f"{amount}"
    elif code in _THREE_DECIMAL_CURRENCIES:
        value = f"{amount / 1000:.3f}"
    else:
        value = f"{amount / 100:.2f}"
    return f"{value} {code.upper()}".strip()


def _price(price: dict) -> str:
    amount = _money(price.get("unit_amount"), price.get("currency"))
    recurring = price.get("recurring") or {}
    interval = recurring.get("interval")
    if not amount or not interval:
        return amount
    count = recurring.get("interval_count") or 1
    return f"{amount} every {count} {interval}s" if count > 1 else f"{amount} per {interval}"


def _day(timestamp: Any) -> str:
    """A unix timestamp as a UTC date. Dates, not times: rows stay stable."""
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        return ""
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")


def _period(start: Any, end: Any) -> str:
    start_day, end_day = _day(start), _day(end)
    return f"{start_day} to {end_day}" if start_day and end_day else ""


def _unix_time(value: datetime | date | str | int | None) -> int | None:
    """Normalize ``created_since`` to a unix timestamp."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("created_since must be a datetime, date, ISO string or unix timestamp")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day, tzinfo=UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp())
