"""Shopify connector for cognee: a ``dlt`` source that turns a Shopify store into memory.

Pulls products and collections and, opt-in, customers and orders through the
GraphQL Admin API, incrementally and with forget-on-delete. The source built
here is handed directly to :func:`cognee.remember`::

    import cognee
    from cognee_community_connector_shopify import shopify_source

    await cognee.remember(
        shopify_source(),               # SHOPIFY_SHOP + credentials from env
        dataset_name="shopify",
        primary_key="id",
        write_disposition="merge",   # incremental upsert by row id
        max_rows_per_table=0,        # 0 = no row cap
    )

Design
------
* **Auth**: a static Admin API access token, or an app's client id and secret
  exchanged for a token at the start of every run (the client credentials
  grant). The token lives in memory only: never in dlt state, files or logs.
  A ``401`` refreshes it once; a second ``401`` stops the run. The connector
  only reads.
* **One GraphQL client** (``_graphql``): every request goes to
  ``https://<shop>.myshopify.com/admin/``, waits out throttling from the cost
  Shopify reports, and raises on a body of the wrong shape or a missing
  ``pageInfo``, so an odd answer is never read as an empty listing.
* **Documents**: one per product (``product:<id>``), collection
  (``collection:<id>``) and, opt-in, customer (``customer:<id>``) and order
  (``order:<id>``). The source declares ``cognee_document_source = "shopify"``,
  so every row goes through normal cognify. Texts carry no timestamps, counters
  or inventory, and every list in them is sorted.
* **Incremental sync**: the sweep (every id with its ``updatedAt``) tells which
  products and customers changed; only those are fetched in full. A collection
  shows its products by title, so each collection is read every run and emitted
  only when its text changed. An order shows its customer and products by name,
  so it is re-rendered when one of them is renamed.
* **Forget-on-delete**: the sweep is the authority. A known id missing from it
  is forgotten only when Shopify confirms it is gone: a ``destroy`` event for
  it, or ``nodes(ids:)`` answering ``null``. Otherwise it is kept and asked
  about again next run. Orders are never swept (only the last 60 days are
  visible, and ageing out is not a deletion); an order is forgotten only on its
  ``destroy`` event.
* **Safety**: every listing is finished before the first row is yielded, and
  state is written only after the last one. A failed listing deletes nothing
  and moves no cursor. An item that fails on its own keeps its document and is
  retried next run; a ``401``, an exhausted throttle or several failures in a
  row abort.
* **Several sources in one dataset**: ``resource_name`` names the dlt table and
  the sync state. Give each store its own name.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("shopify_connector")

# Default dlt resource / staging-table name, and the document-source tag for its rows.
SHOPIFY_TABLE_NAME = "shopify_documents"
SHOPIFY_SOURCE_NAME = "shopify"

# The resources a source can sync. Products and collections are the default;
# customers and orders hold personal data and are opt-in.
RESOURCES = ("products", "collections", "customers", "orders")
DEFAULT_RESOURCES = ("products", "collections")

# The GraphQL Admin API version, pinned in one place (Shopify ships a new
# stable version every quarter and supports each for at least a year).
_API_VERSION = "2026-10"
# Per request: a generous bound for one API call, so a stalled connection fails
# and is retried.
_TIMEOUT_SECONDS = 60
# Shopify's maximum page size for a GraphQL connection.
_PAGE_SIZE = 250
# nodes(ids:) takes at most 250 ids per call.
_NODES_BATCH = 250

# Retry budget for throttled / transient responses: five attempts with
# 1, 2, 4, 8 s back-off between them, or Shopify's own wait on a throttle.
_MAX_RETRIES = 5
# The 5xx answers Shopify's docs describe as temporary.
_TRANSIENT_STATUSES = (500, 502, 503, 504)
# Longest single wait for the throttle bucket to refill. The standard bucket
# holds 1,000 points and refills at 50 a second, so 20 s refills it entirely.
_MAX_THROTTLE_WAIT_SECONDS = 20.0
# Abort when this many items in a row fail on their own. One bad item is kept
# and retried next run, but a streak means Shopify itself is failing, and every
# further item would spend five retries for nothing.
_MAX_FAILURES_IN_A_ROW = 3

# A product document lists at most this many variants (one page of them),
# a collection at most this many products, then says how many more there are.
_MAX_VARIANTS = 100
_MAX_MEMBERS = 250
# A single GraphQL query may request at most 1,000 cost points, and a nested
# connection multiplies: orders(first: N) x lineItems(first: M) costs about
# N * (M + 1). 10 orders x 51 = 510, well inside the limit with room for the
# other fields. An order with more line items says so ("and more line items").
_ORDER_PAGE = 10
_MAX_LINE_ITEMS = 50

# A copy fetched within this long of its updatedAt may still miss a change
# that carries the same timestamp (whole seconds, possibly applied late), so it
# is fetched once more next run. Wide on purpose until the lag is measured.
_SETTLE_MARGIN = timedelta(seconds=60)
# How far before the order cursor the order listing starts, so an order
# stamped at or just before the cursor after the last listing is not missed.
# Orders inside the overlap are listed again; an unchanged text is not emitted.
_ORDER_OVERLAP = timedelta(minutes=5)
# Without read_all_orders, Shopify shows only orders created in the last 60
# days. Name references are kept for those only; older orders cannot be read
# again, so they do not follow renames.
_ORDER_WINDOW = timedelta(days=60)
# Deleted orders are found by asking for them by id (see sync_store), but only
# while they are at least this far inside the window. What nodes() answers for
# an order past the window without read_all_orders was not observable; the
# margin keeps the check well inside it. A wrong deletion is permanent and
# silent, a missed one only keeps an order until it ages out, hence a wide margin.
_ORDER_CHECK_MARGIN = timedelta(days=7)

_SHOP_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ShopifyAPIError(RuntimeError):
    """A non-retryable (or retries-exhausted) error from the Shopify API.

    ``status`` is the HTTP status; a GraphQL ``errors`` answer has status 200.
    ``rate_limited`` is True when the retries ran out while Shopify was still
    throttling.
    """

    def __init__(
        self, status: int, message: str, rate_limited: bool = False, code: str | None = None
    ):
        super().__init__(f"Shopify API returned {status}: {message}")
        self.status = status
        self.rate_limited = rate_limited
        # The GraphQL error code (extensions.code), such as ACCESS_DENIED.
        self.code = code


class ShopifyQueryCostError(RuntimeError):
    """A query asked for more than Shopify's per-query cost limit (MAX_COST_EXCEEDED).

    It fails the same way every time, so it is neither retried nor kept per
    item: the query itself must be made smaller.
    """


class ShopifyResponseError(RuntimeError):
    """Shopify answered with a body that is not JSON or not of the expected shape."""


class ShopifyUnavailableError(RuntimeError):
    """Too many items in a row failed: Shopify is failing, not one item."""


class ShopifySyncStoppedError(RuntimeError):
    """The host's ``check_active`` raised an ``OSError``; the sync must stop.

    Network errors are ``OSError`` too and are retried, or kept per item, so an
    ``OSError`` from the host's check is re-raised as this type, which nothing
    in the connector catches. Other exceptions from the check pass unchanged.
    """


# ---------------------------------------------------------------------------
# GraphQL queries (module constants; each operation is named for the logs)
# ---------------------------------------------------------------------------
_PRODUCT_SWEEP = """
query ProductSweep($after: String) {
  products(first: 250, after: $after, sortKey: ID) {
    nodes { id updatedAt title }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_PRODUCT = """
query Product($id: ID!) {
  product(id: $id) {
    id updatedAt title status vendor productType tags handle onlineStoreUrl description
    options { name values }
    variants(first: 100) { nodes { id title sku price position } }
    variantsCount { count }
  }
}
"""

_COLLECTION_SWEEP = """
query CollectionSweep($after: String) {
  collections(first: 250, after: $after, sortKey: ID) {
    nodes { id }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_COLLECTION = """
query Collection($id: ID!) {
  collection(id: $id) {
    id title handle description
    ruleSet { appliedDisjunctively rules { column relation condition } }
    products(first: 250) { nodes { id } }
    productsCount { count }
  }
}
"""

_CUSTOMER_SWEEP = """
query CustomerSweep($after: String) {
  customers(first: 250, after: $after, sortKey: ID) {
    nodes { id updatedAt displayName }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_CUSTOMER = """
query Customer($id: ID!) {
  customer(id: $id) {
    id updatedAt displayName tags note state
    defaultEmailAddress { emailAddress }
    defaultAddress { city country }
  }
}
"""

_ORDER_FIELDS = """
      id createdAt updatedAt name displayFinancialStatus displayFulfillmentStatus
      customer { id }
      totalPriceSet { shopMoney { amount currencyCode } }
      lineItems(first: 50) { nodes { title quantity product { id } } pageInfo { hasNextPage } }
"""

_ORDERS = (
    """
query Orders($after: String, $query: String) {
  orders(first: 10, after: $after, sortKey: UPDATED_AT, query: $query) {
    nodes {"""
    + _ORDER_FIELDS
    + """    }
    pageInfo { hasNextPage endCursor }
  }
}
"""
)

_ORDER = (
    """
query Order($id: ID!) {
  order(id: $id) {"""
    + _ORDER_FIELDS
    + """  }
}
"""
)

_DESTROY_EVENTS = """
query DestroyEvents($after: String, $query: String) {
  events(first: 250, after: $after, sortKey: ID, query: $query) {
    nodes { id ... on BasicEvent { action subjectId subjectType } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_LATEST_EVENT = """
query LatestEvent($query: String) {
  events(first: 1, sortKey: ID, reverse: true, query: $query) {
    nodes { id }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_NODES = """
query Nodes($ids: [ID!]!) {
  nodes(ids: $ids) { id }
}
"""

# A rough upper bound of each query's cost, to wait for the throttle bucket
# before sending it (Shopify charges about one point per requested node).
_COSTS = {
    _PRODUCT_SWEEP: 260,
    _PRODUCT: 110,
    _COLLECTION_SWEEP: 260,
    _COLLECTION: 260,
    _CUSTOMER_SWEEP: 260,
    _CUSTOMER: 5,
    _ORDERS: 560,
    _ORDER: 60,
    _DESTROY_EVENTS: 260,
    _LATEST_EVENT: 5,
    _NODES: 5,  # 1 point for 250 ids, seen live
}

# Subject types whose destroy events matter, mapped to the row-id prefix.
_DESTROY_KINDS = {
    "PRODUCT": "product",
    "COLLECTION": "collection",
    "CUSTOMER": "customer",
    "ORDER": "order",
}


# ---------------------------------------------------------------------------
# Shop and URLs
# ---------------------------------------------------------------------------
def normalize_shop(shop: str) -> str:
    """``name`` or ``name.myshopify.com`` (any case) -> ``name.myshopify.com``.

    Anything else is refused, custom domains included: the token is only ever
    sent to the shop's own ``myshopify.com`` host.
    """
    value = (shop or "").strip().lower().removeprefix("https://").rstrip("/")
    name = value.removesuffix(".myshopify.com")
    if not _SHOP_PATTERN.match(name):
        raise ValueError(
            f"shop must be the store's myshopify name, like 'acme' or 'acme.myshopify.com'; "
            f"got {shop!r}"
        )
    return f"{name}.myshopify.com"


def _admin_url(shop: str, path: str) -> str:
    """The absolute URL of an Admin path, refusing anything outside the shop's admin."""
    url = f"https://{shop}/admin/{path.lstrip('/')}"
    if not url.startswith(f"https://{shop}/admin/"):  # pragma: no cover - defensive
        raise ValueError(f"refusing to call a URL outside the shop's admin: {url}")
    return url


def _numeric_id(gid: str) -> str:
    """``gid://shopify/Product/123`` -> ``123``."""
    return str(gid).rsplit("/", 1)[-1]


def _gid(kind: str, numeric: str) -> str:
    """``("product", "123")`` -> ``gid://shopify/Product/123``."""
    return f"gid://shopify/{kind.capitalize()}/{numeric}"


def _row_id(kind: str, gid: str) -> str:
    return f"{kind}:{_numeric_id(gid)}"


# ---------------------------------------------------------------------------
# Auth and the GraphQL client
# ---------------------------------------------------------------------------
def _make_session() -> Any:
    """A plain ``requests`` session. Credentials go per request, never on it."""
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - requests is a declared dependency
        raise ImportError(
            'The Shopify connector requires "requests". Install the package:\n'
            "    pip install cognee-community-connector-shopify"
        ) from exc
    return requests.Session()


class _Client:
    """One run's connection to a shop: the token, the throttle state, the session.

    The token is held here and only here. It is never logged, never put in an
    exception message and never written to state.
    """

    def __init__(
        self,
        session: Any,
        shop: str,
        access_token: str | None,
        client_id: str | None,
        client_secret: str | None,
    ):
        self.session = session
        self.shop = shop
        self._static_token = access_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = access_token
        # The bucket as Shopify last reported it: (available, restore rate per s).
        self._bucket: tuple[float, float] | None = None
        # The API version Shopify said it served (X-Shopify-API-Version), and the
        # highest requested cost seen per operation; both for the logs.
        self.served_version: str | None = None
        self.requested_costs: dict[str, float] = {}
        # Shopify's clock at its first answer this run (the Date header): the
        # order window is measured on it, so a skewed local clock cannot move it.
        self.server_now: datetime | None = None

    @property
    def has_budget_figures(self) -> bool:
        return self._bucket is not None

    @property
    def can_refresh(self) -> bool:
        return self._static_token is None

    def token(self) -> str:
        if self._token is None:
            self._token = self._fetch_token()
        return self._token

    def refresh(self) -> None:
        self._token = self._fetch_token()

    def _fetch_token(self) -> str:
        """The client credentials grant: exchange the app's id and secret for a token."""
        response = self.session.post(
            _admin_url(self.shop, "oauth/access_token"),
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise ShopifyAPIError(
                response.status_code,
                f"the client credentials grant was refused ({_grant_error(response)})",
            )
        token = _json_object(response).get("access_token")
        if not isinstance(token, str) or not token:
            raise ShopifyResponseError("the client credentials answer carried no access_token")
        return token

    def wait_for(self, cost: float) -> None:
        """Sleep until the throttle bucket last reported can pay for ``cost``."""
        if self._bucket is None:
            return
        available, rate = self._bucket
        if available >= cost or rate <= 0:
            return
        wait = min((cost - available) / rate, _MAX_THROTTLE_WAIT_SECONDS)
        logger.info("Shopify: waiting %.1fs for the query cost budget.", wait)
        time.sleep(wait)
        self._bucket = None

    def note_version(self, response: Any) -> None:
        """Warn once if Shopify serves another API version than the one asked for.

        An unknown or retired version is not an error: Shopify answers from a
        different one, whose fields may differ from what the queries expect.
        """
        served = (response.headers or {}).get("X-Shopify-API-Version")
        if served and self.served_version is None:
            self.served_version = served
            if served != _API_VERSION:
                logger.warning(
                    "Shopify: asked for API version %s, served %s.", _API_VERSION, served
                )

    def note_cost(self, extensions: Any, operation: str = "") -> None:
        cost = (extensions or {}).get("cost") or {}
        try:
            requested = float(cost["requestedQueryCost"])
            self.requested_costs[operation] = max(self.requested_costs.get(operation, 0), requested)
        except (KeyError, TypeError, ValueError):
            pass
        status = cost.get("throttleStatus") or {}
        try:
            self._bucket = (float(status["currentlyAvailable"]), float(status["restoreRate"]))
        except (KeyError, TypeError, ValueError):
            self._bucket = None


def _grant_error(response: Any) -> str:
    """Shopify's ``error`` code from a refused grant, never any credential."""
    try:
        body = response.json()
        return str(body.get("error") or body.get("errors") or "no detail")[:120]
    except (ValueError, AttributeError):
        return "no detail"


def _json_object(response: Any) -> dict:
    """The JSON object of an answer; anything else raises."""
    try:
        body = response.json()
    except ValueError as exc:
        raise ShopifyResponseError(f"Shopify answered {response.status_code} without JSON") from exc
    if not isinstance(body, dict):
        raise ShopifyResponseError(
            f"Shopify answered {response.status_code} with a {type(body).__name__}, not an object"
        )
    return body


def _server_time(response: Any) -> datetime | None:
    """Shopify's clock when it answered, from the ``Date`` response header."""
    try:
        return parsedate_to_datetime((response.headers or {}).get("Date"))
    except (TypeError, ValueError):
        return None


def _retry_after(response: Any, attempt: int) -> float:
    try:
        return float((response.headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return float(2**attempt)


def _graphql(client: _Client, query: str, variables: dict) -> tuple[dict, datetime | None]:
    """Run one GraphQL query and return ``(data, server time)``.

    Waits for the throttle bucket before sending. On ``401`` the token is
    refreshed once (client credentials only) and the query repeated; a second
    ``401`` raises. ``429``, a ``THROTTLED`` error, 5xx answers and network
    errors are retried up to ``_MAX_RETRIES`` times. Any other ``errors`` entry
    raises, even with HTTP 200, and so does a body without a ``data`` object.
    """
    client.wait_for(_COSTS.get(query, 100))
    url = _admin_url(client.shop, f"api/{_API_VERSION}/graphql.json")
    refreshed = False
    attempt = 0
    while True:
        last_attempt = attempt >= _MAX_RETRIES - 1
        try:
            response = client.session.post(
                url,
                json={"query": query, "variables": variables},
                headers={
                    "X-Shopify-Access-Token": client.token(),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            # requests' connection and timeout errors all derive from OSError.
            if last_attempt:
                raise
            delay = float(2**attempt)
            logger.warning("Shopify: %s; retrying in %.1fs.", type(exc).__name__, delay)
            time.sleep(delay)
            attempt += 1
            continue

        status = response.status_code
        if status == 401:
            if client.can_refresh and not refreshed:
                logger.info("Shopify: access token refused; fetching a new one.")
                client.refresh()
                refreshed = True
                continue
            raise ShopifyAPIError(401, "the access token was refused")
        if status == 429 or status in _TRANSIENT_STATUSES:
            if last_attempt:
                raise ShopifyAPIError(status, "still failing after retries", status == 429)
            delay = _retry_after(response, attempt)
            logger.warning("Shopify: HTTP %d; retrying in %.1fs.", status, delay)
            time.sleep(delay)
            attempt += 1
            continue
        if status != 200:
            raise ShopifyAPIError(status, _error_text(response))

        body = _json_object(response)
        client.note_version(response)
        if client.server_now is None:
            client.server_now = _server_time(response)
        client.note_cost(body.get("extensions"), _operation(query))
        errors = body.get("errors")
        if errors:
            if "MAX_COST_EXCEEDED" in _error_codes(errors):
                raise ShopifyQueryCostError(
                    f"Shopify refused the {_operation(query)} query as too costly; "
                    f"{_errors_text(errors)}"
                )
            if _is_throttled(errors):
                if last_attempt:
                    raise ShopifyAPIError(200, "THROTTLED after retries", rate_limited=True)
                if client.has_budget_figures:
                    client.wait_for(_COSTS.get(query, 100))
                else:  # no budget figures to wait on: back off instead
                    time.sleep(float(2**attempt))
                attempt += 1
                continue
            codes = _error_codes(errors)
            raise ShopifyAPIError(200, _errors_text(errors), code=codes[0] if codes else None)
        data = body.get("data")
        if not isinstance(data, dict):
            raise ShopifyResponseError("Shopify's answer has no data object")
        return data, _server_time(response)


def _operation(query: str) -> str:
    """The operation name of a query constant, for logs and errors."""
    match = re.search(r"query (\w+)", query)
    return match.group(1) if match else "query"


def _error_codes(errors: Any) -> list[str]:
    if not isinstance(errors, list):
        return []
    return [
        str((e.get("extensions") or {}).get("code"))
        for e in errors
        if isinstance(e, dict) and (e.get("extensions") or {}).get("code")
    ]


def _is_throttled(errors: Any) -> bool:
    return isinstance(errors, list) and all(
        isinstance(e, dict) and (e.get("extensions") or {}).get("code") == "THROTTLED"
        for e in errors
    )


def _errors_text(errors: Any) -> str:
    if isinstance(errors, list):
        return "; ".join(str(e.get("message") if isinstance(e, dict) else e) for e in errors)[:300]
    return str(errors)[:300]


def _error_text(response: Any) -> str:
    try:
        return _errors_text(response.json().get("errors"))
    except (ValueError, AttributeError):
        return str(getattr(response, "text", ""))[:200]


def _connection(data: dict, field: str) -> tuple[list[dict], str | None]:
    """``(nodes, next cursor)`` of a connection; a missing part raises.

    A missing ``pageInfo`` is never taken for the last page: that would read a
    cut listing as complete, and the missing ids as deletions.
    """
    connection = data.get(field)
    if not isinstance(connection, dict):
        raise ShopifyResponseError(f"Shopify's answer has no {field} connection")
    nodes = connection.get("nodes")
    page = connection.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(page, dict):
        raise ShopifyResponseError(f"Shopify's {field} connection has no nodes or pageInfo")
    if page.get("hasNextPage"):
        cursor = page.get("endCursor")
        if not cursor:
            raise ShopifyResponseError(f"Shopify's {field} says there is more but gives no cursor")
        return nodes, cursor
    return nodes, None


def _list_all(client: _Client, query: str, field: str, variables: dict | None = None) -> list[dict]:
    """Every node of a connection, page by page. Never a partial list."""
    nodes: list[dict] = []
    after: str | None = None
    while True:
        data, _ = _graphql(client, query, {**(variables or {}), "after": after})
        page, after = _connection(data, field)
        nodes.extend(page)
        if after is None:
            return nodes


def _single(client: _Client, query: str, field: str, gid: str) -> tuple[dict, datetime | None]:
    """One object by id; ``null`` (gone between the sweep and now) raises like any failure."""
    data, fetched_at = _graphql(client, query, {"id": gid})
    item = data.get(field)
    if not isinstance(item, dict):
        raise ShopifyResponseError(f"Shopify answered no {field} for {gid}")
    return item, fetched_at


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return str(value or "").strip()


def _render_product(product: dict) -> str:
    """A product as text: header, description, options, variants. No inventory.

    Deterministic for unchanged input: tags and option values keep Shopify's
    meaning but are sorted where order carries none, variants follow their
    position. No timestamps or counters, so an unchanged product keeps its
    content hash and is not re-cognified.
    """
    lines = ["Type: product", f"Status: {_text(product.get('status')).lower() or 'unknown'}"]
    for label, key in (("Vendor", "vendor"), ("Product type", "productType")):
        if _text(product.get(key)):
            lines.append(f"{label}: {_text(product[key])}")
    tags = sorted(_text(tag) for tag in product.get("tags") or [] if _text(tag))
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")
    if _text(product.get("handle")):
        lines.append(f"Handle: {_text(product['handle'])}")
    if _text(product.get("onlineStoreUrl")):
        lines.append(f"URL: {_text(product['onlineStoreUrl'])}")
    options = sorted(
        (_text(o.get("name")), ", ".join(_text(v) for v in o.get("values") or []))
        for o in product.get("options") or []
        if _text(o.get("name"))
    )
    # A product without variants has one option "Title" with "Default Title".
    options = [(name, values) for name, values in options if values != "Default Title"]
    for name, values in options:
        lines.append(f"Option {name}: {values}")

    sections = ["\n".join(lines)]
    if _text(product.get("description")):
        sections.append(_text(product["description"]))
    variants = sorted(
        (product.get("variants") or {}).get("nodes") or [],
        key=lambda v: (v.get("position") or 0, _numeric_order(v.get("id"))),
    )
    shown = [v for v in variants if _text(v.get("title")) != "Default Title"] or variants
    if shown:
        out = []
        for variant in shown[:_MAX_VARIANTS]:
            parts = [_text(variant.get("title")) or "Default"]
            if _text(variant.get("sku")):
                parts.append(f"SKU {_text(variant['sku'])}")
            if _text(variant.get("price")):
                parts.append(f"price {_text(variant['price'])}")
            out.append("- " + ", ".join(parts))
        total = _count(product.get("variantsCount"), len(variants))
        if total > len(out):
            out.append(f"- ... and {total - len(out)} more variants")
        sections.append("Variants:\n" + "\n".join(out))
    return "\n\n".join(sections)


def _render_collection(collection: dict, titles: dict[str, str]) -> str:
    """A collection as text: kind and rules, description, its products by title.

    ``titles`` maps product gids to titles from this run's product sweep, so a
    renamed product changes the text of every collection that lists it.
    """
    rule_set = collection.get("ruleSet")
    lines = ["Type: collection"]
    if isinstance(rule_set, dict):
        joiner = " or " if rule_set.get("appliedDisjunctively") else " and "
        rules = sorted(
            " ".join(_text(r.get(k)).lower() for k in ("column", "relation", "condition"))
            for r in rule_set.get("rules") or []
        )
        lines.append(f"Kind: smart ({joiner.join(rules)})" if rules else "Kind: smart")
    else:
        lines.append("Kind: custom")
    if _text(collection.get("handle")):
        lines.append(f"Handle: {_text(collection['handle'])}")

    sections = ["\n".join(lines)]
    if _text(collection.get("description")):
        sections.append(_text(collection["description"]))
    members = (collection.get("products") or {}).get("nodes") or []
    names = sorted(_text(titles.get(m.get("id"))) for m in members if titles.get(m.get("id")))
    if names:
        out = [f"- {name}" for name in names[:_MAX_MEMBERS]]
        total = _count(collection.get("productsCount"), len(members))
        if total > len(out):
            out.append(f"- ... and {total - len(out)} more products")
        sections.append("Products:\n" + "\n".join(out))
    return "\n\n".join(sections)


def _render_customer(customer: dict) -> str:
    """A customer as text: name, email, state, tags, note, city and country only.

    No phone, no street address and no order counters or amounts spent: those
    are not needed to answer questions about the store and change on every order.
    """
    lines = ["Type: customer"]
    email = _text((customer.get("defaultEmailAddress") or {}).get("emailAddress"))
    if email:
        lines.append(f"Email: {email}")
    if _text(customer.get("state")):
        lines.append(f"State: {_text(customer['state']).lower()}")
    address = customer.get("defaultAddress") or {}
    place = ", ".join(_text(address.get(k)) for k in ("city", "country") if _text(address.get(k)))
    if place:
        lines.append(f"Location: {place}")
    tags = sorted(_text(tag) for tag in customer.get("tags") or [] if _text(tag))
    if tags:
        lines.append(f"Tags: {', '.join(tags)}")
    sections = ["\n".join(lines)]
    if _text(customer.get("note")):
        sections.append(_text(customer["note"]))
    return "\n\n".join(sections)


def _order_names(order: dict) -> tuple[str | None, list[str]]:
    """The customer gid and product gids an order shows by name."""
    customer = (order.get("customer") or {}).get("id")
    items = (order.get("lineItems") or {}).get("nodes") or []
    products = sorted({(i.get("product") or {}).get("id") for i in items} - {None})
    return customer, products


def _render_order(order: dict, customers: dict[str, str], titles: dict[str, str]) -> str:
    """An order as text: status, customer and line items by name and id, total.

    ``customers`` and ``titles`` come from this run's sweeps, so a renamed
    customer or product changes the text of the orders that show them.
    """
    lines = ["Type: order"]
    for label, key in (
        ("Payment", "displayFinancialStatus"),
        ("Fulfillment", "displayFulfillmentStatus"),
    ):
        if _text(order.get(key)):
            lines.append(f"{label}: {_text(order[key]).lower().replace('_', ' ')}")
    customer_gid, _ = _order_names(order)
    if customer_gid:
        # The name comes from the customer sweep, which runs only when customers
        # are selected; otherwise the order names its customer by id alone.
        name = customers.get(customer_gid)
        row_id = _row_id("customer", customer_gid)
        lines.append(f"Customer: {name} ({row_id})" if name else f"Customer: {row_id}")
    money = (order.get("totalPriceSet") or {}).get("shopMoney") or {}
    if _text(money.get("amount")):
        lines.append(f"Total: {_text(money['amount'])} {_text(money.get('currencyCode'))}".strip())
    sections = ["\n".join(lines)]
    items = []
    for item in (order.get("lineItems") or {}).get("nodes") or []:
        product = (item.get("product") or {}).get("id")
        title = (titles.get(product) if product else None) or _text(item.get("title"))
        suffix = f" ({_row_id('product', product)})" if product else ""
        items.append(f"- {item.get('quantity') or 0} x {title}{suffix}")
    if items:
        lines = sorted(items)[:_MAX_LINE_ITEMS]
        if ((order.get("lineItems") or {}).get("pageInfo") or {}).get("hasNextPage"):
            lines.append(f"- ... and more line items (over {_MAX_LINE_ITEMS})")
        sections.append("Line items:\n" + "\n".join(lines))
    return "\n\n".join(sections)


def _count(value: Any, default: int) -> int:
    try:
        return int((value or {}).get("count"))
    except (TypeError, ValueError):
        return default


def _numeric_order(gid: Any) -> int:
    try:
        return int(_numeric_id(gid))
    except (TypeError, ValueError):
        return 0


def _hash(text: str) -> str:
    """A short, stable hash of a document's text, small enough to keep in state."""
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def _row(row_id: str, title: str, url: str, content: str) -> dict[str, Any]:
    return {"id": row_id, "url": url, "title": title.strip(), "content": content, "_deleted": False}


def _deleted_row(row_id: str) -> dict[str, Any]:
    """Build a minimal row that instructs dlt to hard-delete a document by id."""
    return {"id": row_id, "_deleted": True}


def _is_settled(updated_at: str, fetched_at: datetime | None) -> bool:
    """True when a copy fetched at ``fetched_at`` is final for its ``updatedAt``."""
    if fetched_at is None or not updated_at:
        return False
    return datetime.fromisoformat(updated_at) <= fetched_at - _SETTLE_MARGIN


# ---------------------------------------------------------------------------
# Per-item failures
# ---------------------------------------------------------------------------
class _ItemFailures:
    """The items of one run that failed on their own and are kept for a retry.

    Only per-item failures are kept: a :class:`ShopifyAPIError`, a
    :class:`ShopifyResponseError` or an ``OSError`` that outlasted the retries.
    A ``401`` (after the one refresh) or an exhausted throttle would fail every
    later item the same way, so it is re-raised, and so is the
    ``_MAX_FAILURES_IN_A_ROW``-th failure in a row. Anything else
    (``check_active``'s errors among them) is never caught in the first place.
    """

    def __init__(self) -> None:
        self.in_a_row = 0
        self.kept = 0

    def keep(self, exc: Exception, what: str) -> None:
        """Count a per-item failure and log it, or raise if it is systemic."""
        if isinstance(exc, ShopifyAPIError) and (exc.status == 401 or exc.rate_limited):
            raise exc
        self.in_a_row += 1
        if self.in_a_row >= _MAX_FAILURES_IN_A_ROW:
            raise ShopifyUnavailableError(
                f"{self.in_a_row} items in a row failed, the last {what}; stopping"
            ) from exc
        why = f"HTTP {exc.status}" if isinstance(exc, ShopifyAPIError) else type(exc).__name__
        self.note_kept(what, why)

    def note_kept(self, what: str, why: str) -> None:
        self.kept += 1
        logger.warning("Shopify: kept %s for a retry next run (%s).", what, why)

    def succeeded(self) -> None:
        self.in_a_row = 0


# What a per-item request can fail with once the retries are spent.
_ITEM_ERRORS = (ShopifyAPIError, ShopifyResponseError, OSError)


# ---------------------------------------------------------------------------
# Deletions
# ---------------------------------------------------------------------------
def _destroyed(client: _Client, cursor: str) -> tuple[dict[str, set[str]], str]:
    """Row ids named by destroy events since ``cursor``, and the new cursor.

    The first run only finds where the event feed ends: nothing is known yet,
    so nothing can need confirming. If the token may not read events, the run
    goes on without them (one warning): deletions are still confirmed through
    ``nodes(ids:)``, at one extra request per run.
    """
    try:
        return _read_destroyed(client, cursor)
    except ShopifyAPIError as exc:
        if exc.status != 403 and exc.code != "ACCESS_DENIED":
            raise
        logger.warning(
            "Shopify: the token may not read events (HTTP %d, %s); deletions are confirmed "
            "through nodes() only.",
            exc.status,
            exc.code or "no code",
        )
        return {}, cursor


def _read_destroyed(client: _Client, cursor: str) -> tuple[dict[str, set[str]], str]:
    if not cursor:
        data, _ = _graphql(client, _LATEST_EVENT, {"query": "action:destroy"})
        nodes, _ = _connection(data, "events")
        return {}, _numeric_id(nodes[0]["id"]) if nodes else "0"
    events = _list_all(
        client, _DESTROY_EVENTS, "events", {"query": f"action:destroy AND id:>{cursor}"}
    )
    found: dict[str, set[str]] = {}
    newest = int(cursor)
    for event in events:
        newest = max(newest, _numeric_order(event.get("id")))
        kind = _DESTROY_KINDS.get(_text(event.get("subjectType")).upper())
        if kind and event.get("action") == "destroy" and event.get("subjectId"):
            found.setdefault(kind, set()).add(f"{kind}:{_numeric_id(event['subjectId'])}")
    return found, str(newest)


def _confirm_gone(
    client: _Client,
    missing: list[str],
    destroyed: set[str],
    failures: _ItemFailures,
) -> set[str]:
    """Which of the ``missing`` row ids are confirmed gone: a destroy event or a null node.

    An id that still resolves, or that cannot be checked, is kept and asked
    about again next run.
    """
    gone = {row_id for row_id in missing if row_id in destroyed}
    unknown = [row_id for row_id in missing if row_id not in gone]
    null, resolving = _null_nodes(client, unknown, failures)
    for row_id in resolving:
        failures.note_kept(row_id, "missing from the listing but still resolving")
    return gone | null


def _null_nodes(
    client: _Client, row_ids: list[str], failures: _ItemFailures
) -> tuple[set[str], list[str]]:
    """Ask ``nodes(ids:)`` about ``row_ids``: those answering null, and those resolving.

    A batch that fails is kept (in neither result) and asked about next run.
    """
    null: set[str] = set()
    resolving: list[str] = []
    for start in range(0, len(row_ids), _NODES_BATCH):
        batch = row_ids[start : start + _NODES_BATCH]
        gids = [_gid(*row_id.split(":", 1)) for row_id in batch]
        try:
            data, _ = _graphql(client, _NODES, {"ids": gids})
            nodes = data.get("nodes")
            if not isinstance(nodes, list) or len(nodes) != len(gids):
                raise ShopifyResponseError("nodes(ids:) did not answer one entry per id")
        except _ITEM_ERRORS as exc:
            failures.keep(exc, f"{len(batch)} id(s) to check")
            continue
        failures.succeeded()
        for row_id, node in zip(batch, nodes, strict=True):
            if node is None:
                null.add(row_id)
            else:
                resolving.append(row_id)
    return null, resolving


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict, so it is unit-testable)
# ---------------------------------------------------------------------------
def sync_store(
    client: _Client,
    state: dict,
    *,
    resources: Iterable[str] = DEFAULT_RESOURCES,
    now: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed documents since the last run, plus hard-delete markers.

    ``state`` holds ``known_ids`` (every row id emitted and still current), a
    ``stamps`` map (product or customer row id -> the ``updatedAt`` of its
    settled copy, or ``""`` to fetch it again), a ``texts`` map (collection or
    order row id -> hash of its last text, ``""`` to emit it again), ``orders``
    (order row id -> the gids it shows by name and a hash of those names, for
    orders inside the 60-day window), the ``order_cursor`` and the destroy
    ``event_cursor``. Never a token. ``now`` (for tests) defaults to Shopify's
    clock, read from the run's first answer.

    1. *List.* The sweeps (all products, collections and, if selected,
       customers), the destroy events and, for orders, the change listing.
       Missing ids are confirmed gone or kept. A failed listing propagates
       before a single row is yielded.
    2. *Render.* Each changed product and customer, every collection (emitted
       when its text changed), each changed order. One that fails on its own
       keeps its old document and is retried next run.
    3. *Delete.* A hard-delete marker for every confirmed deletion.
    4. *Commit.* Only now write the new state.
    """
    selected = [r for r in RESOURCES if r in set(resources)]
    known_ids: set[str] = set(state.get("known_ids", []))
    stamps: dict[str, str] = dict(state.get("stamps", {}))
    texts: dict[str, str] = dict(state.get("texts", {}))
    order_refs: dict[str, dict] = dict(state.get("orders", {}))
    failures = _ItemFailures()

    # --- 1. List ------------------------------------------------------------
    # Products are swept whenever collections or orders need their titles.
    need_products = {"products", "collections", "orders"} & set(selected)
    products = _list_all(client, _PRODUCT_SWEEP, "products") if need_products else []
    titles = {p["id"]: _text(p.get("title")) for p in products}
    collections = (
        _list_all(client, _COLLECTION_SWEEP, "collections") if "collections" in selected else []
    )
    customers = _customer_sweep(client) if "customers" in selected else []
    names = {c["id"]: _text(c.get("displayName")) for c in customers}
    destroyed, event_cursor = _destroyed(client, state.get("event_cursor", ""))

    swept: dict[str, list[dict]] = {
        "product": products if "products" in selected else [],
        "collection": collections,
        "customer": customers if "customers" in selected else [],
    }
    current_ids: set[str] = set()
    for kind, items in swept.items():
        current_ids |= {_row_id(kind, item["id"]) for item in items}
    kinds_swept = {"product": "products", "collection": "collections", "customer": "customers"}
    missing = sorted(
        row_id
        for row_id in known_ids - current_ids
        if kinds_swept.get(row_id.split(":", 1)[0]) in selected
    )
    all_destroyed = set().union(*destroyed.values()) if destroyed else set()
    gone = _confirm_gone(client, missing, all_destroyed, failures)
    current_ids |= set(missing) - gone

    order_rows: list[dict] = []
    order_cursor = state.get("order_cursor", "")
    if "orders" in selected:
        now = now or _shop_now(client)
        query = None
        if order_cursor:
            since = datetime.fromisoformat(order_cursor) - _ORDER_OVERLAP
            query = f"updated_at:>='{_stamp(since)}'"
        order_rows = _list_all(client, _ORDERS, "orders", {"query": query})
        # Orders are never swept: a known one stays current until confirmed deleted.
        current_ids |= {row_id for row_id in known_ids if row_id.startswith("order:")}
        current_ids |= {_row_id("order", order["id"]) for order in order_rows}
        current_ids -= destroyed.get("order", set())
        # Deleting an order leaves no destroy event (seen live), so each known
        # order not listed this run is asked about by id while it is at least
        # _ORDER_CHECK_MARGIN inside the window; a null answer means it was
        # deleted. Older orders are kept: null could mean they aged out.
        listed_ids = {_row_id("order", order["id"]) for order in order_rows}
        checkable = _stamp(now - _ORDER_WINDOW + _ORDER_CHECK_MARGIN)
        unlisted = sorted(
            row_id
            for row_id, refs in order_refs.items()
            if row_id in current_ids
            and row_id not in listed_ids
            and refs.get("created", "") >= checkable
        )
        deleted_orders, _ = _null_nodes(client, unlisted, failures)
        current_ids -= deleted_orders

    # --- 2. Render ----------------------------------------------------------
    rendered = 0
    for product in swept["product"]:
        row_id = _row_id("product", product["id"])
        if stamps.get(row_id) == product.get("updatedAt") and row_id in known_ids:
            continue
        try:
            full, fetched_at = _single(client, _PRODUCT, "product", product["id"])
        except _ITEM_ERRORS as exc:
            failures.keep(exc, row_id)
            stamps[row_id] = ""  # no row: the old document stays, and is fetched next run
            continue
        failures.succeeded()
        settled = _is_settled(_text(full.get("updatedAt")), fetched_at)
        stamps[row_id] = _text(full.get("updatedAt")) if settled else ""
        rendered += 1
        yield _row(
            row_id,
            _text(full.get("title")),
            _text(full.get("onlineStoreUrl")),
            _render_product(full),
        )

    for customer in swept["customer"]:
        row_id = _row_id("customer", customer["id"])
        if stamps.get(row_id) == customer.get("updatedAt") and row_id in known_ids:
            continue
        try:
            full, fetched_at = _single(client, _CUSTOMER, "customer", customer["id"])
        except _ITEM_ERRORS as exc:
            failures.keep(exc, row_id)
            stamps[row_id] = ""
            continue
        failures.succeeded()
        settled = _is_settled(_text(full.get("updatedAt")), fetched_at)
        stamps[row_id] = _text(full.get("updatedAt")) if settled else ""
        rendered += 1
        yield _row(row_id, _text(full.get("displayName")), "", _render_customer(full))

    for collection in swept["collection"]:
        # A collection is read every run: its text shows its members by title,
        # and neither a membership change nor a product rename is visible in the
        # collection listing. Emitted only when the text changed.
        row_id = _row_id("collection", collection["id"])
        try:
            full, _ = _single(client, _COLLECTION, "collection", collection["id"])
        except _ITEM_ERRORS as exc:
            failures.keep(exc, row_id)
            texts[row_id] = ""
            continue
        failures.succeeded()
        content = _render_collection(full, titles)
        if texts.get(row_id) == _hash(content) and row_id in known_ids:
            continue
        texts[row_id] = _hash(content)
        rendered += 1
        yield _row(row_id, _text(full.get("title")), "", content)

    if "orders" in selected:
        listed = {_row_id("order", order["id"]): order for order in order_rows}
        # An order shows its customer and products by name. One that was not
        # listed this run (unchanged) is fetched again when a name it shows
        # changed: its stored references are checked against this run's sweeps.
        renamed = sorted(
            row_id
            for row_id, refs in order_refs.items()
            if row_id not in listed
            and row_id in current_ids
            and refs.get("names") != _names_hash(refs.get("ids") or [], names, titles)
        )
        for row_id in renamed:
            try:
                listed[row_id], _ = _single(client, _ORDER, "order", _gid(*row_id.split(":", 1)))
            except _ITEM_ERRORS as exc:
                failures.keep(exc, row_id)
                continue
            failures.succeeded()
        for row_id, order in sorted(listed.items()):
            content = _render_order(order, names, titles)
            ids = _flat_refs(order)
            order_refs[row_id] = {
                "created": _text(order.get("createdAt")),
                "ids": ids,
                "names": _names_hash(ids, names, titles),
            }
            if texts.get(row_id) == _hash(content) and row_id in known_ids:
                continue
            texts[row_id] = _hash(content)
            rendered += 1
            yield _row(row_id, _text(order.get("name")), "", content)
        if order_rows:
            order_cursor = max([order_cursor, *(_text(o.get("updatedAt")) for o in order_rows)])
        # References are kept only while the order can still be read again.
        oldest = _stamp(now - _ORDER_WINDOW)
        order_refs = {k: v for k, v in order_refs.items() if v.get("created", "") >= oldest}

    # --- 3. Delete ----------------------------------------------------------
    deleted = known_ids - current_ids
    for row_id in sorted(deleted):
        yield _deleted_row(row_id)

    # --- 4. Commit ----------------------------------------------------------
    state["known_ids"] = sorted(current_ids)
    state["stamps"] = {k: v for k, v in sorted(stamps.items()) if k in current_ids}
    state["texts"] = {k: v for k, v in sorted(texts.items()) if k in current_ids}
    state["orders"] = {k: v for k, v in sorted(order_refs.items()) if k in current_ids}
    state["event_cursor"] = event_cursor
    state["order_cursor"] = order_cursor
    logger.info(
        "Shopify: %d document(s) rendered, %d deletion(s) across %s; kept %d item(s) for retry.",
        rendered,
        len(deleted),
        ", ".join(selected),
        failures.kept,
    )


def _shop_now(client: _Client) -> datetime:
    """Shopify's clock at the run's first answer, or the local clock (logged) without one."""
    if client.server_now is not None:
        return client.server_now
    logger.warning("Shopify: no Date header seen; measuring the order window on the local clock.")
    return _local_now()


def _local_now() -> datetime:
    return datetime.now(UTC)


def _customer_sweep(client: _Client) -> list[dict]:
    """Every customer id, ``updatedAt`` and name; a clear error if the app may not read them.

    Customer data is protected: an app needs Shopify's approval (or, on a dev
    store, the protected customer data settings) to read it. Without that the
    sweep answers ACCESS_DENIED, and the run stops before anything is deleted.
    """
    try:
        return _list_all(client, _CUSTOMER_SWEEP, "customers")
    except ShopifyAPIError as exc:
        if exc.code != "ACCESS_DENIED":
            raise
        raise ShopifyAPIError(
            exc.status,
            "the app may not read customers or their name and email (protected customer "
            "data); in the Dev Dashboard give the app customer access with the Name and Email "
            "fields, or leave 'customers' out of resources",
            code=exc.code,
        ) from exc


def _flat_refs(order: dict) -> list[str]:
    """The gids an order shows by name: its customer (or ``""``), then its products."""
    customer, products = _order_names(order)
    return [customer or "", *products]


def _names_hash(ids: list[str], names: dict[str, str], titles: dict[str, str]) -> str:
    """A hash of the names an order borrows, as this run's sweeps give them."""
    customer, *products = ids or [""]
    borrowed = [names.get(customer, "")] + [titles.get(p, "") for p in products]
    return _hash("\n".join(borrowed))


def _stamp(moment: datetime) -> str:
    """Format a moment the way Shopify does (UTC, whole seconds, ``Z``)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def shopify_source(
    shop: str | None = None,
    *,
    access_token: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    resources: Iterable[str] = DEFAULT_RESOURCES,
    resource_name: str = SHOPIFY_TABLE_NAME,
    check_active: Callable[[], None] | None = None,
    session: Any = None,
):
    """Return a ``dlt`` resource that yields Shopify documents for ``remember``.

    Args:
        shop: The store, as ``name`` or ``name.myshopify.com``. Falls back to
            ``SHOPIFY_SHOP``.
        access_token: A static Admin API access token. Falls back to
            ``SHOPIFY_ACCESS_TOKEN``.
        client_id, client_secret: An app's credentials, exchanged for a token
            at the start of every run. Fall back to ``SHOPIFY_CLIENT_ID`` and
            ``SHOPIFY_CLIENT_SECRET``. Used when no access token is given.
        resources: What to sync: ``"products"``, ``"collections"`` (the
            default) and, opt-in because they hold personal data,
            ``"customers"`` and ``"orders"``.
        resource_name: Stable dlt resource (table) name, which also scopes the
            sync state. Give each store its own name when several sync into one
            dataset; sharing a name would make each run forget the other's
            documents.
        check_active: Optional host authorization checkpoint. It is called
            before every Shopify request and around every yielded row, and
            stops the sync by raising; nothing is deleted and no state moves.
        session: Pre-built ``requests`` session. Mainly an injection point for
            tests; when omitted one is built.

    Returns:
        A ``dlt`` resource (``resource_name``) configured with
        ``primary_key="id"``, ``write_disposition="merge"`` and an ``_deleted``
        hard-delete column. Hand it to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:  # pragma: no cover - dlt is a declared dependency
        raise ImportError(
            'The Shopify connector requires "dlt". Install the package:\n'
            "    pip install cognee-community-connector-shopify"
        ) from exc

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 1:
        raise RuntimeError(
            "The Shopify connector requires a cognee build with table-scoped dlt document "
            "cleanup (cognee>=1.6). Upgrade cognee so upstream deletions are forgotten."
        )

    resolved_shop = normalize_shop(shop or os.environ.get("SHOPIFY_SHOP", ""))
    if isinstance(resources, str):
        resources = [resources]
    chosen = list(dict.fromkeys(resources))
    unknown = [r for r in chosen if r not in RESOURCES]
    if unknown or not chosen:
        raise ValueError(f"resources must be some of {RESOURCES}; got {list(resources)!r}")

    token = access_token or os.environ.get("SHOPIFY_ACCESS_TOKEN")
    cid = client_id or os.environ.get("SHOPIFY_CLIENT_ID")
    secret = client_secret or os.environ.get("SHOPIFY_CLIENT_SECRET")
    if not token and not (cid and secret):
        raise ValueError(
            "Shopify credentials required: pass access_token= (or set SHOPIFY_ACCESS_TOKEN), "
            "or client_id= and client_secret= (or set SHOPIFY_CLIENT_ID and "
            "SHOPIFY_CLIENT_SECRET)."
        )

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # _deleted is a boolean hard-delete marker: rows where it is True are
        # removed from the dlt destination on merge, which propagates the
        # deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def shopify_documents():
        http = session or _make_session()
        check = _stop_on(check_active) if check_active is not None else None
        if check is not None:
            # The listing phase makes many requests before the first row.
            http = _GuardedSession(http, check)
        client = _Client(
            http, resolved_shop, token, None if token else cid, None if token else secret
        )
        yield from dlt_utils.guarded_rows(
            sync_store(client, dlt.current.resource_state(), resources=chosen), check
        )

    resource = shopify_documents()
    # Opt into the document ingestion path (row -> text document -> cognify).
    # resolve_dlt_sources reads this marker; it never imports this connector.
    setattr(resource, DOCUMENT_SOURCE_ATTR, SHOPIFY_SOURCE_NAME)
    # Give the incremental state its own dlt pipeline per dataset and name.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    return resource


def _stop_on(check_active: Callable[[], None]) -> Callable[[], None]:
    """``check_active``, turning an ``OSError`` it raises into ``ShopifySyncStoppedError``."""

    def check() -> None:
        try:
            check_active()
        except OSError as exc:
            raise ShopifySyncStoppedError(f"check_active stopped the sync: {exc!r}") from exc

    return check


class _GuardedSession:
    """A session that asks the host's ``check_active`` before every request."""

    def __init__(self, session: Any, check_active: Callable[[], None]):
        self._session = session
        self._check_active = check_active

    def post(self, *args: Any, **kwargs: Any) -> Any:
        self._check_active()
        return self._session.post(*args, **kwargs)
