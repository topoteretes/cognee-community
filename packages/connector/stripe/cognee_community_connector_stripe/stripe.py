"""DLT source that syncs Stripe billing data into cognee as documents.

Reads customers, subscriptions, invoices, charges, products and disputes of one
Stripe account. After the first full listing it follows Stripe's events, and an
object deleted in Stripe is forgotten. The README has the design notes::

    await cognee.remember(
        stripe_source(api_key, account_id="acct_..."),
        dataset_name="stripe",
        primary_key="id",
        write_disposition="merge",  # required: the source emits deletions
    )
"""

import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import dlt
import stripe
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils

logger = get_logger("stripe_connector")

STRIPE_SOURCE_NAME = "stripe"

# Pinned, so every connected account returns the same object shapes whatever
# its own default API version is.
STRIPE_API_VERSION = "2025-03-31.basil"

# Stripe requests one run may make before it stops cleanly and resumes next run.
DEFAULT_MAX_REQUESTS = 5_000

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
_STATE_VERSION = 2

# The first cognee release whose document sources take per-row node sets.
_MIN_DOCUMENT_SYNC_VERSION = 2

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
    # The events that name this type, as a ``type`` filter with a wildcard.
    event_type: str
    dashboard_path: str
    # History objects honour ``created_since``; the others are always whole.
    windowed: bool
    list_params: dict[str, Any]
    render: Callable[[dict, "RenderOptions"], dict[str, Any]]
    # Extra parameters for retrieving one object (the list form of ``expand``
    # is prefixed with ``data.``, so each endpoint spells it out).
    retrieve_params: dict[str, Any] = field(default_factory=dict)


def stripe_client(
    api_key: str,
    *,
    account_id: str | None = None,
    http_client: Any = None,
) -> stripe.StripeClient:
    """Return a ``StripeClient`` scoped to one account.

    Args:
        api_key: The Connect platform's secret key, or a restricted key with
            read access to read your own account.
        account_id: The connected account (``acct_...``) sent as
            ``Stripe-Account``. Omit it to read the key's own account.
        http_client: A ``stripe`` HTTP client (mainly a test-injection point).
    """
    if not api_key:
        raise ValueError("A Stripe API key is required.")
    return stripe.StripeClient(
        api_key,
        stripe_account=account_id or None,
        stripe_version=STRIPE_API_VERSION,
        max_network_retries=_MAX_NETWORK_RETRIES,
        http_client=http_client,
    )


def stripe_source(
    api_key: str,
    *,
    account_id: str | None = None,
    objects: Sequence[str] | None = None,
    created_since: datetime | date | str | int | None = None,
    include_contact_details: bool = False,
    include_metadata: bool = False,
    full_refresh: bool = False,
    resource_name: str | None = None,
    max_requests: int | None = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
    http_client: Any = None,
):
    """Create a dlt source that yields Stripe objects as documents.

    Args:
        api_key: The Connect platform's secret key, or a restricted read key of
            your own account.
        account_id: The connected account to read (``acct_...``).
        objects: What to ingest, a list of any of ``customers``,
            ``subscriptions``, ``invoices``, ``charges``, ``products`` and
            ``disputes`` (default: all). An empty list is valid and forgets
            everything synced before.
        created_since: Only keep invoices, charges and disputes created at or
            after this time (a datetime, date, ISO string or unix timestamp).
            Older ones are forgotten. The other types are always synced whole.
        include_contact_details: Also ingest customer email and phone.
        include_metadata: Also ingest each object's free-form ``metadata``.
        full_refresh: List everything this run instead of reading events.
        resource_name: Names the dlt resource, the pipeline scope and the
            staging tables, so it must differ for every account synced into
            one dataset. Defaults to a name derived from ``account_id``.
        max_requests: Stripe requests one run may make. A run that spends them
            stops cleanly, keeps what it read and resumes next run. ``None``
            lifts the limit.
        check_active: Called before every request and row; raises to stop a
            sync whose connection was revoked (used by the cognee integration).
        http_client: A ``stripe`` HTTP client (mainly a test-injection point).

    Returns:
        A dlt source for ``cognee.remember(...)``, with the counts of the last
        run in ``source.cognee_sync_stats``. Pass ``write_disposition="merge"``;
        any other disposition is refused.
    """
    _require_document_sync()
    if isinstance(objects, str):
        raise TypeError(
            f"objects must be a list of object types, e.g. [{objects!r}], not a string."
        )
    selected = list(dict.fromkeys(OBJECT_TYPES if objects is None else objects))
    unknown = [name for name in selected if name not in OBJECT_TYPES]
    if unknown:
        raise ValueError(
            f"Unknown Stripe object type(s) {unknown}; choose from {sorted(OBJECT_TYPES)}."
        )
    name = _resource_name(resource_name, account_id)
    client = stripe_client(api_key, account_id=account_id, http_client=http_client)
    options = RenderOptions(
        include_contact_details=include_contact_details, include_metadata=include_metadata
    )
    since = _unix_time(created_since)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the staging table on merge,
        # which is what makes cognee's orphan cleanup forget them.
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            **dlt_utils.NODE_SET_COLUMN_HINT,
        },
    )
    def stripe_objects():
        # The caller's write_disposition overrides the one declared above. A
        # replace run of an event delta would empty every table but the
        # changed rows, so refuse it before reading anything.
        disposition = source.resources[name].write_disposition
        if disposition != "merge":
            raise ValueError(
                f'stripe_source needs write_disposition="merge", got {disposition!r}. '
                "Its incremental runs emit only changes and deletions."
            )
        rows = sync_objects(
            client,
            selected,
            dlt.current.resource_state(),
            resource_name=name,
            created_since=since,
            options=options,
            full_refresh=full_refresh,
            stats=stats,
            check_active=check_active,
            max_requests=max_requests,
        )
        for table_name, row in dlt_utils.guarded_rows(rows, check_active):
            yield dlt.mark.with_table_name(row, table_name)

    @dlt.source(name=STRIPE_SOURCE_NAME)
    def _stripe():
        return stripe_objects

    source = _stripe()
    # Opt into the document path (object → text document → cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(source, dlt_utils.DOCUMENT_SOURCE_ATTR, STRIPE_SOURCE_NAME)
    # One dlt pipeline, and so one cursor, per resource name.
    setattr(source, dlt_utils.PIPELINE_SCOPE_ATTR, name)
    source.cognee_sync_stats = stats
    return source


def _require_document_sync() -> None:
    """Fail clearly on a cognee that ignores per-row node sets."""
    version = getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0)
    if version < _MIN_DOCUMENT_SYNC_VERSION:
        raise RuntimeError(
            "The Stripe connector needs a cognee whose document sources take per-row node "
            f"sets (DOCUMENT_SYNC_VERSION >= {_MIN_DOCUMENT_SYNC_VERSION}, cognee 1.6.4 or "
            f"later), but this one reports {version}."
        )


def _resource_name(resource_name: str | None, account_id: str | None) -> str:
    """The dlt resource name: explicit, else derived from the account so accounts stay apart."""
    if resource_name is not None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", resource_name):
            raise ValueError(
                f"resource_name {resource_name!r} must be lowercase letters, digits and "
                "underscores, starting with a letter."
            )
        return resource_name
    if not account_id:
        return STRIPE_SOURCE_NAME
    suffix = re.sub(r"[^a-z0-9]+", "_", account_id.lower()).strip("_")
    return f"{STRIPE_SOURCE_NAME}_{suffix}"


# ---------------------------------------------------------------------------
# Requests: every Stripe call of a run goes through one place
# ---------------------------------------------------------------------------


class _SyncStopError(Exception):
    """A run that ends cleanly: ``reason`` is the stats key that says why."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Requests:
    """Checks the connection before each Stripe request and counts it against the budget.

    A spent budget or a rate limit that outlasts the client's retries stops the
    run cleanly. An authentication or permission error is recorded and raised.
    """

    def __init__(
        self,
        client: stripe.StripeClient,
        check_active: Callable[[], None] | None,
        max_requests: int | None,
        stats: dict[str, int],
    ):
        self.client = client
        self._check_active = check_active
        self._max_requests = max_requests
        self._stats = stats

    @property
    def count(self) -> int:
        return self._stats["requests"]

    @property
    def max_requests(self) -> int | None:
        return self._max_requests

    def call(self, request: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._check_active is not None:
            self._check_active()
        if self._max_requests is not None and self.count >= self._max_requests:
            raise _SyncStopError("failed_budget")
        self._stats["requests"] += 1
        try:
            return request(*args, **kwargs)
        except stripe.RateLimitError:
            raise _SyncStopError("failed_rate_limit") from None
        except (stripe.AuthenticationError, stripe.PermissionError):
            self._stats["failed_auth"] = 1
            self._stats["failed"] = 1
            raise

    def list(
        self, service: Any, params: dict[str, Any], after: str | None = None
    ) -> Iterator[dict]:
        """Yield every object of a Stripe list endpoint as a plain dict.

        The dlt verified source's paging loop: pages of 100, each one starting
        after the last id of the page before, or after ``after`` to resume.
        """
        starting_after = after
        while True:
            page_params = {**params, "limit": _PAGE_SIZE}
            if starting_after:
                page_params["starting_after"] = starting_after
            page = self.call(service.list, params=page_params)
            data = [obj.to_dict() for obj in page.data]
            yield from data
            if not page.has_more or not data:
                return
            starting_after = data[-1]["id"]

    def retrieve(self, service: Any, object_id: str, params: dict[str, Any]) -> dict | None:
        """The object as a dict, or None when it was deleted."""
        try:
            obj = self.call(service.retrieve, object_id, params=params).to_dict()
        except stripe.InvalidRequestError as error:
            if error.http_status == 404:
                return None
            raise
        # Deleted customers stay retrievable as a stub that says so.
        return None if obj.get("deleted") else obj

    def service(self, object_type: _ObjectType) -> Any:
        return getattr(self.client.v1, object_type.name)


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict — unit-testable)
# ---------------------------------------------------------------------------


def _table_name(resource_name: str, object_type: _ObjectType) -> str:
    return f"{resource_name}_{object_type.name}"


def sync_objects(
    client: stripe.StripeClient,
    objects: Sequence[str],
    state: dict,
    *,
    resource_name: str = STRIPE_SOURCE_NAME,
    created_since: int | None = None,
    options: RenderOptions | None = None,
    full_refresh: bool = False,
    now: float | None = None,
    stats: dict[str, int] | None = None,
    check_active: Callable[[], None] | None = None,
    max_requests: int | None = DEFAULT_MAX_REQUESTS,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(table_name, row)`` for everything that changed since the last run.

    ``state`` holds the events ``cursor`` (when the last finished sync started),
    the ``scope`` it covered, the ``known`` ids per object type and, while a
    full listing is unfinished, the ``resume`` point. dlt only persists it when
    the run loads, so a failed run leaves it as it was and the next run repeats
    the work. ``stats`` is filled with ``written`` and ``deleted`` objects, the
    ``requests`` made and, when the run did not finish, ``failed`` and its
    reason (``failed_budget``, ``failed_rate_limit`` or ``failed_auth``).
    """
    options = options or RenderOptions()
    stats = {} if stats is None else stats
    stats.clear()
    stats.update(written=0, deleted=0, requests=0, failed=0)
    requests = _Requests(client, check_active, max_requests, stats)
    scope = {
        "version": _STATE_VERSION,
        "objects": sorted(objects),
        "created_since": created_since,
        "contact": options.include_contact_details,
        "metadata": options.include_metadata,
    }
    previous_known: dict[str, list[str]] = state.get("known") or {}
    resume = state.get("resume") if (state.get("resume") or {}).get("scope") == scope else None
    cursor = state.get("cursor")
    # A run that resumes a full listing keeps that listing's start as its cursor.
    started = resume["started"] if resume else int(time.time() if now is None else now)
    incremental = (
        resume is None
        and not full_refresh
        and state.get("scope") == scope
        and isinstance(cursor, int)
        and started - cursor < _EVENT_RETENTION_SECONDS
    )

    changed: dict[str, set[str]] = {}
    if incremental and objects:
        # With nothing selected there is nothing to refresh, so no events are read.
        try:
            changed = _changed_ids(requests, objects, cursor - _CURSOR_OVERLAP_SECONDS)
        except _EventsTooLongError:
            incremental = False
        except _SyncStopError as stop:
            _record_stop(stats, stop)
            return

    queue = list(resume["queue"]) if resume else list(objects)
    # The point a resumed listing starts from; only its first type has one.
    progress = {"after": resume["after"], "listed": set(resume["listed"])} if resume else {}
    known = {name: set(previous_known.get(name) or []) for name in objects}

    for position, name in enumerate(queue):
        object_type = OBJECT_TYPES[name]
        ids = changed.get(object_type.stripe_object, set())
        listing = not incremental or len(ids) > _MAX_RETRIEVES_PER_TYPE
        if listing:
            rows = _sync_all(requests, object_type, known[name], created_since, options, progress)
        else:
            rows = _sync_changed(requests, object_type, ids, known[name], created_since, options)
        try:
            for row in rows:
                if row["_deleted"]:
                    known[name].discard(row["id"])
                    stats["deleted"] += 1
                else:
                    known[name].add(row["id"])
                    stats["written"] += 1
                yield _table_name(resource_name, object_type), row
        except _SyncStopError as stop:
            # A listing resumes where it stopped. Anything else starts over as a
            # full listing, which is bounded by the budget and can always resume.
            after = progress.get("after") if listing else None
            listed = progress.get("listed", set()) if listing else set()
            state["resume"] = {
                "scope": scope,
                "started": started,
                "queue": queue[position:],
                "after": after,
                "listed": sorted(listed),
            }
            # Types that left the selection are tombstoned only by a run that finishes,
            # so their ids must outlive one that stops.
            state["known"] = {
                **{n: ids for n, ids in previous_known.items() if n not in known},
                **{n: sorted(ids) for n, ids in known.items()},
            }
            _record_stop(stats, stop)
            return
        if listing:
            # A finished listing is the whole truth about the type.
            known[name] = progress["listed"]
        progress = {}

    # Types that left the selection are forgotten.
    for name, ids in previous_known.items():
        if name in known or name not in OBJECT_TYPES:
            continue
        for object_id in ids:
            stats["deleted"] += 1
            yield _table_name(resource_name, OBJECT_TYPES[name]), _tombstone(object_id)

    state.pop("resume", None)
    state["cursor"] = started
    state["scope"] = scope
    state["known"] = {name: sorted(ids) for name, ids in known.items()}
    logger.info(
        "Stripe: %s sync of %s: %d object(s) written, %d forgotten, %d request(s).",
        "incremental" if incremental else "full",
        ", ".join(objects) or "nothing",
        stats["written"],
        stats["deleted"],
        stats["requests"],
    )


def _record_stop(stats: dict[str, int], stop: _SyncStopError) -> None:
    stats["failed"] = 1
    stats[stop.reason] = 1
    logger.warning(
        "Stripe: sync stopped after %d request(s) (%s); what was read is kept and the "
        "next run resumes.",
        stats["requests"],
        stop.reason,
    )


def _sync_all(
    requests: _Requests,
    object_type: _ObjectType,
    old_ids: set[str],
    created_since: int | None,
    options: RenderOptions,
    progress: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """List every object of a type; tombstone known ids that were not listed.

    ``progress`` carries the last id read and the ids listed so far, so a run
    that stops can resume from there. It ends holding every id listed.
    """
    params = dict(object_type.list_params)
    if object_type.windowed and created_since is not None:
        params["created"] = {"gte": created_since}
    progress.setdefault("after", None)
    progress.setdefault("listed", set())
    for obj in requests.list(requests.service(object_type), params, after=progress["after"]):
        yield _row(object_type, obj, options)
        progress["listed"].add(obj["id"])
        progress["after"] = obj["id"]
    for object_id in sorted(old_ids - progress["listed"]):
        yield _tombstone(object_id)


def _sync_changed(
    requests: _Requests,
    object_type: _ObjectType,
    ids: set[str],
    old_ids: set[str],
    created_since: int | None,
    options: RenderOptions,
) -> Iterator[dict[str, Any]]:
    """Re-read the objects events named; tombstone the ones that are gone."""
    service = requests.service(object_type)
    for object_id in sorted(ids):
        obj = requests.retrieve(service, object_id, object_type.retrieve_params)
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


class _EventsTooLongError(Exception):
    """The events to read cost more than a full listing would."""


def _changed_ids(requests: _Requests, objects: Sequence[str], since: int) -> dict[str, set[str]]:
    """Ids of the objects named by events since ``since``, keyed by object type.

    Stripe filters on ``type`` here, which takes a trailing wildcard
    (``customer.*``); ``types`` does not, and takes at most 20 names. Only the
    event families of the selected types are read. The walk gives up when it
    alone would take half the budget, so a full listing can resume instead.
    """
    event_types = _event_types(objects)
    limit = None if requests.max_requests is None else max(1, requests.max_requests // 2)
    start = requests.count
    changed: dict[str, set[str]] = {}
    for event_type in event_types:
        params = {"created": {"gte": since}, "type": event_type}
        for event in requests.list(requests.client.v1.events, params):
            if limit is not None and requests.count - start > limit:
                raise _EventsTooLongError
            obj = (event.get("data") or {}).get("object") or {}
            # An upcoming invoice has no id yet; there is nothing to re-read.
            if obj.get("id") and obj.get("object") in _STRIPE_OBJECTS:
                changed.setdefault(obj["object"], set()).add(obj["id"])
            # A charge's document says whether it is disputed, but a dispute's events
            # name only the dispute, so its charge is refreshed with it.
            if obj.get("object") == "dispute" and _id(obj.get("charge")):
                changed.setdefault("charge", set()).add(_id(obj["charge"]))
    return changed


def _event_types(objects: Sequence[str]) -> list[str]:
    """The event ``type`` filters for the selected types, without a family another one covers."""
    types = {OBJECT_TYPES[name].event_type for name in objects}
    return sorted(
        event_type
        for event_type in types
        if not any(
            other != event_type and event_type.startswith(other.removesuffix("*"))
            for other in types
        )
    )


def _row(object_type: _ObjectType, obj: dict, options: RenderOptions) -> dict[str, Any]:
    row = object_type.render(obj, options)
    test_mode = "" if obj.get("livemode") else "/test"
    return {
        "id": obj["id"],
        "title": row["title"],
        "content": row["content"],
        "url": f"https://dashboard.stripe.com{test_mode}/{object_type.dashboard_path}/{obj['id']}",
        dlt_utils.NODE_SET_COLUMN: sorted({node for node in row["node_sets"] if node}),
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
    of_customer = f" of customer {customer_id}" if customer_id else ""
    lines = [
        f"Stripe subscription {subscription['id']}{of_customer}.",
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
    subscription_details = (invoice.get("parent") or {}).get("subscription_details") or {}
    subscription_id = _id(subscription_details.get("subscription"))
    currency = invoice.get("currency")
    label = invoice.get("number") or invoice["id"]
    transitions = invoice.get("status_transitions") or {}
    for_customer = f" for customer {customer_id}" if customer_id else ""
    lines = [
        f"Stripe invoice {label} ({invoice['id']}){for_customer}.",
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
    node_sets = [_node("customer", customer_id), _node("subscription", subscription_id)]
    # A charge does not name its invoice. The invoice's payments name the
    # PaymentIntent (or charge) that paid it, and the charge names its
    # PaymentIntent, so both documents share that node set. Stripe returns an
    # invoice's first 10 payments inline, so a longer retry history links only those.
    payment_intents = []
    for invoice_payment in (invoice.get("payments") or {}).get("data") or []:
        payment = invoice_payment.get("payment") or {}
        payment_intent_id = _id(payment.get("payment_intent"))
        if payment_intent_id:
            payment_intents.append(payment_intent_id)
        node_sets += [
            _node("payment_intent", payment_intent_id),
            _node("charge", _id(payment.get("charge"))),
        ]
    lines.append(_field("Payment intent", ", ".join(payment_intents)))
    line_items = invoice.get("lines") or {}
    entries = line_items.get("data") or []
    if entries:
        lines.append("Lines:")
    for entry in entries:
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
    if line_items.get("has_more"):
        lines.append("- (more lines in Stripe)")
    lines += _metadata(invoice, options)
    return {
        "title": f"Invoice {label} ({invoice.get('status') or 'unknown'})",
        "content": _join(lines),
        "node_sets": node_sets,
    }


def render_charge(charge: dict, options: RenderOptions) -> dict[str, Any]:
    customer_id = _id(charge.get("customer"))
    payment_intent_id = _id(charge.get("payment_intent"))
    currency = charge.get("currency")
    amount = _money(charge.get("amount"), currency)
    of_customer = f" of customer {customer_id}" if customer_id else ""
    lines = [
        f"Stripe charge {charge['id']}{of_customer} for {amount}.",
        _field("Status", charge.get("status")),
        _field("Description", charge.get("description")),
        _field("Payment intent", payment_intent_id),
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
            _node("payment_intent", payment_intent_id),
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
        "node_sets": [_node("charge", charge_id)],
    }


OBJECT_TYPES: dict[str, _ObjectType] = {
    object_type.name: object_type
    for object_type in (
        _ObjectType(
            name="customers",
            event_type="customer.*",
            stripe_object="customer",
            dashboard_path="customers",
            windowed=False,
            list_params={},
            render=render_customer,
        ),
        _ObjectType(
            name="subscriptions",
            event_type="customer.subscription.*",
            stripe_object="subscription",
            dashboard_path="subscriptions",
            windowed=False,
            # Without status=all Stripe leaves canceled subscriptions out.
            list_params={"status": "all"},
            render=render_subscription,
        ),
        _ObjectType(
            name="invoices",
            event_type="invoice.*",
            stripe_object="invoice",
            dashboard_path="invoices",
            windowed=True,
            # An invoice names the charge that paid it only through its payments,
            # which Stripe leaves out unless asked.
            list_params={"expand": ["data.payments"]},
            retrieve_params={"expand": ["payments"]},
            render=render_invoice,
        ),
        _ObjectType(
            name="charges",
            event_type="charge.*",
            stripe_object="charge",
            dashboard_path="payments",
            windowed=True,
            list_params={},
            render=render_charge,
        ),
        _ObjectType(
            name="products",
            event_type="product.*",
            stripe_object="product",
            dashboard_path="products",
            windowed=False,
            list_params={},
            render=render_product,
        ),
        _ObjectType(
            name="disputes",
            event_type="charge.dispute.*",
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


def _id(value: str | None) -> str | None:
    return value or None


def _line_product(entry: dict) -> str | None:
    price_details = (entry.get("pricing") or {}).get("price_details") or {}
    return _id(price_details.get("product"))


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


def _money(amount: int | None, currency: str | None) -> str:
    """Render a Stripe amount (in the currency's smallest unit) as ``49.00 USD``."""
    if amount is None:
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


def _day(timestamp: int | None) -> str:
    """A unix timestamp as a UTC date. Dates, not times: rows stay stable."""
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")


def _period(start: int | None, end: int | None) -> str:
    start_day, end_day = _day(start), _day(end)
    return f"{start_day} to {end_day}" if start_day and end_day else ""


def _unix_time(value: datetime | date | str | int | None) -> int | None:
    """Normalize ``created_since`` to a unix timestamp."""
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())
