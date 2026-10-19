"""Shopify connector for cognee: a ``dlt`` source that turns a Shopify store into memory.

Products and collections and, opt-in, customers and orders, read through the
GraphQL Admin API, synced incrementally with forget-on-delete. Hand the source
to :func:`cognee.remember`::

    await cognee.remember(
        shopify_source(),  # SHOPIFY_SHOP + credentials from env
        dataset_name="shopify",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

The README describes what is stored, how changes and deletions are found, and
what a run does when Shopify fails part of the way through.
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

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

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
# The longest single wait of any kind. cognee holds its dlt staging lock during
# a sync, so a longer Retry-After stops the run cleanly instead (it resumes next run).
_MAX_WAIT_SECONDS = 60
# Fresh item failures in a row (5xx or no answer, after retries) that mean
# Shopify itself is down: the run stops cleanly. Items kept from an earlier run
# never count.
_OUTAGE_STREAK = 3
# Runs an item may stay unreadable (403, ACCESS_DENIED, or null while still
# listed) at the same listing stamp before its document is forgotten. It is
# asked again once its stamp moves.
_GIVE_UP_RUNS = 3

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

_STAT_KEYS = ("scanned", "skipped", "failed", "deleted", "kept", "given_up")


class ShopifyAPIError(RuntimeError):
    """An error from the Shopify API on ``endpoint`` (an operation name, or the token grant).

    ``status`` is the HTTP status: 200 for a GraphQL ``errors`` answer (whose
    ``code`` is set, such as ``ACCESS_DENIED``), 0 for no answer at all.
    ``rate_limited`` is True when Shopify was still throttling. The message
    names only the status or code and the endpoint, plus the connector's own
    advice where it has some; Shopify's explanation is on ``detail``. A
    response body or a credential is never in either.
    """

    def __init__(
        self,
        status: int,
        endpoint: str,
        detail: str = "",
        *,
        rate_limited: bool = False,
        code: str | None = None,
        hint: str = "",
    ):
        if status == 0:
            answer = "no answer"
        elif status == 200:
            answer = code or "GraphQL error"
        else:
            answer = f"HTTP {status}"
        super().__init__(f"Shopify {endpoint}: {answer}" + (f"; {hint}" if hint else ""))
        self.status = status
        self.endpoint = endpoint
        self.detail = detail
        self.rate_limited = rate_limited
        self.code = code
        self.hint = hint


class ShopifyQueryCostError(RuntimeError):
    """A query asked for more than Shopify's per-query cost limit (MAX_COST_EXCEEDED).

    It fails the same way every time, so it is neither retried nor kept per
    item: the query itself must be made smaller. Shopify's text is on ``detail``.
    """

    def __init__(self, operation: str, detail: str = ""):
        super().__init__(f"Shopify refused the {operation} query as too costly")
        self.detail = detail


class ShopifyResponseError(RuntimeError):
    """Shopify answered with a body that is not JSON or not of the expected shape.

    Never read as an empty listing or an item to skip: it aborts the run.
    """


class _CutShortError(Exception):
    """Internal: stop the run cleanly, keeping what was rendered so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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
    nodes { id updatedAt }
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
def _make_session() -> requests.Session:
    """A plain ``requests`` session. Credentials go per request, never on it."""
    return requests.Session()


def _check_secret(value: str, what: str) -> None:
    """Refuse a credential ``requests`` would reject, without ever echoing it.

    A pasted token often ends in a newline; requests then raises an error whose
    message holds the whole header, token included.
    """
    if any(char.isspace() or not char.isprintable() for char in value):
        raise ValueError(f"The Shopify {what} contains whitespace or control characters")


class _Client:
    """One run's connection to a shop: the token, the throttle state, the session.

    The token is held here and only here. It is never logged, never put in an
    exception message and never written to state. ``check_active`` is asked
    before every request and every wait.
    """

    def __init__(
        self,
        session: Any,
        shop: str,
        access_token: str | None,
        client_id: str | None,
        client_secret: str | None,
        check_active: Callable[[], None] | None = None,
    ):
        self.session = session
        self.shop = shop
        self._static_token = access_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = access_token
        self.check_active = check_active
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

    def check(self) -> None:
        if self.check_active is not None:
            self.check_active()

    def wait(self, seconds: float) -> None:
        self.check()
        time.sleep(seconds)

    def token(self) -> str:
        if self._token is None:
            self._token = self._fetch_token()
        return self._token

    def refresh(self) -> None:
        self._token = self._fetch_token()

    def _fetch_token(self) -> str:
        """The client credentials grant: exchange the app's id and secret for a token."""
        endpoint = "oauth/access_token"
        self.check()
        try:
            response = self.session.post(
                _admin_url(self.shop, endpoint),
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Accept": "application/json"},
                timeout=_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ShopifyAPIError(0, endpoint, type(exc).__name__) from None
        if response.status_code != 200:
            raise ShopifyAPIError(response.status_code, endpoint, _grant_error(response))
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
        self.wait(wait)
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
    """Shopify's ``error`` code from a refused grant (``invalid_client``), or nothing."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return ""
    return error[:120] if isinstance(error, str) else ""


def _json_object(response: Any) -> dict:
    """The JSON object of an answer; anything else raises."""
    try:
        body = response.json()
    except ValueError:
        status = response.status_code
        raise ShopifyResponseError(f"Shopify answered {status} without JSON") from None
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
    errors are retried up to ``_MAX_RETRIES`` times; a wait over
    ``_MAX_WAIT_SECONDS`` is not taken. Any other ``errors`` entry raises, even
    with HTTP 200, and so does a body without a ``data`` object.
    """
    operation = _operation(query)
    client.wait_for(_COSTS.get(query, 100))
    url = _admin_url(client.shop, f"api/{_API_VERSION}/graphql.json")
    refreshed = False
    attempt = 0
    while True:
        last_attempt = attempt >= _MAX_RETRIES - 1
        client.check()
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
        except requests.RequestException as exc:
            # Only the type: the message of some requests errors holds a header.
            if last_attempt:
                raise ShopifyAPIError(0, operation, type(exc).__name__) from None
            delay = float(2**attempt)
            logger.warning(
                "Shopify: %s on %s; retrying in %.0fs.", type(exc).__name__, operation, delay
            )
            client.wait(delay)
            attempt += 1
            continue

        status = response.status_code
        if status == 401:
            if client.can_refresh and not refreshed:
                logger.info("Shopify: access token refused; fetching a new one.")
                client.refresh()
                refreshed = True
                continue
            raise ShopifyAPIError(401, operation)
        if status == 429 or status in _TRANSIENT_STATUSES:
            if last_attempt:
                raise ShopifyAPIError(status, operation, rate_limited=status == 429)
            delay = _retry_after(response, attempt)
            if delay > _MAX_WAIT_SECONDS:
                raise ShopifyAPIError(
                    status, operation, f"asked to wait {delay:.0f}s", rate_limited=status == 429
                )
            logger.warning("Shopify: HTTP %d on %s; retrying in %.0fs.", status, operation, delay)
            client.wait(delay)
            attempt += 1
            continue
        if status != 200:
            raise ShopifyAPIError(status, operation, _error_text(response))

        body = _json_object(response)
        client.note_version(response)
        if client.server_now is None:
            client.server_now = _server_time(response)
        client.note_cost(body.get("extensions"), operation)
        errors = body.get("errors")
        if errors:
            if "MAX_COST_EXCEEDED" in _error_codes(errors):
                raise ShopifyQueryCostError(operation, _errors_text(errors))
            if _is_throttled(errors):
                if last_attempt:
                    raise ShopifyAPIError(200, operation, rate_limited=True, code="THROTTLED")
                if client.has_budget_figures:
                    client.wait_for(_COSTS.get(query, 100))
                else:  # no budget figures to wait on: back off instead
                    client.wait(float(2**attempt))
                attempt += 1
                continue
            codes = _error_codes(errors)
            raise ShopifyAPIError(
                200, operation, _errors_text(errors), code=codes[0] if codes else None
            )
        data = body.get("data")
        if not isinstance(data, dict):
            raise ShopifyResponseError(f"Shopify's answer to {operation} has no data object")
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
    """Shopify's ``errors`` of an error answer, or nothing. Never the raw body."""
    try:
        errors = response.json().get("errors")
    except (ValueError, AttributeError):
        return ""
    return _errors_text(errors) if errors else ""


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
    """One object by id. ``null`` raises a ``NOT_FOUND`` :class:`ShopifyAPIError`."""
    data, fetched_at = _graphql(client, query, {"id": gid})
    item = data.get(field)
    if item is None:
        # Still listed, yet not found: not proof it is gone (see _Run._failed).
        raise ShopifyAPIError(200, _operation(query), f"no {field}", code="NOT_FOUND")
    if not isinstance(item, dict):
        raise ShopifyResponseError(f"Shopify answered a {field} that is not an object")
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
        # Unlike data queries, no answer here is read as content or as gone; events
        # are only a hint, nodes() still confirms, so a denial is a warning, not an abort.
        if exc.status != 403 and exc.code != "ACCESS_DENIED":
            raise
        logger.warning(
            "Shopify: the token may not read events (%s); deletions are confirmed "
            "through nodes() only.",
            exc,
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


def _is_unreadable(exc: ShopifyAPIError) -> bool:
    """A 404 or a null for an item that is still listed: kept, never proof of deletion."""
    return exc.status == 404 or exc.code == "NOT_FOUND"


def _raise_if_access_denied(exc: ShopifyAPIError) -> None:
    """A GraphQL ACCESS_DENIED is the app's field access or scopes, never one item: abort.

    Treating it per item would forget every item of that kind after a few runs.
    """
    if exc.code != "ACCESS_DENIED" or exc.hint:
        return
    field = re.search(r"use the (\w+) field", exc.detail or "")
    what = f"the {field.group(1)} field" if field else "a field this query asks for"
    raise ShopifyAPIError(
        exc.status,
        exc.endpoint,
        exc.detail,
        code=exc.code,
        hint=f"the app may not read {what}; check the app's scopes and protected customer "
        "data fields in the Dev Dashboard",
    ) from None


# ---------------------------------------------------------------------------
# Sync (pure given a client + state dict, so it is unit-testable)
# ---------------------------------------------------------------------------
class _Run:
    """One sync of the selected resources; ``state`` changes only at the end.

    State: ``known_ids`` (row ids emitted and current), ``stamps`` (product or
    customer row id -> ``updatedAt`` of its settled copy, ``""`` to fetch it
    again), ``texts`` (collection or order row id -> hash of its last text),
    ``orders`` (order row id -> the gids it shows by name and a hash of those
    names), ``retry`` (items kept after a failure), ``unreadable`` (row id ->
    ``{stamp, runs}``), ``given_up`` (row id -> the stamp at which an unreadable
    item was forgotten), ``order_cursor`` and the destroy ``event_cursor``.
    """

    def __init__(
        self,
        client: _Client,
        state: dict,
        stats: dict[str, int],
        selected: list[str],
        now: datetime | None,
    ):
        self.client = client
        self.state = state
        self.stats = stats
        self.selected = selected
        self.now = now
        self.known_ids: set[str] = set(state.get("known_ids", []))
        self.stamps: dict[str, str] = dict(state.get("stamps", {}))
        self.texts: dict[str, str] = dict(state.get("texts", {}))
        self.order_refs: dict[str, dict] = dict(state.get("orders", {}))
        self.retry: set[str] = set(state.get("retry", []))
        self.unreadable: dict[str, dict] = {
            row_id: dict(entry) for row_id, entry in state.get("unreadable", {}).items()
        }
        self.given_up: dict[str, str] = dict(state.get("given_up", {}))
        self.event_cursor: str = state.get("event_cursor", "")
        self.order_cursor: str = state.get("order_cursor", "")
        self.titles: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.listing: dict[str, str] = {}  # row id -> its listing stamp this run
        self.current_ids: set[str] = set()
        self.order_rows: list[dict] = []
        self.refetched: dict[str, dict] = {}
        self.work: list[tuple[str, str, str]] = []  # (kind, row id, gid): fresh first
        self.position = 0  # index in ``work`` of the next item to fetch
        self.kept: set[str] = set()  # failed on its own this run
        self.ambiguous: set[str] = set()  # missing from the sweep, still resolving
        self.rendered: set[str] = set()
        self.fetched_kinds: set[str] = set()  # kinds with a successful fetch this run
        self.forbidden: dict[str, list[str]] = {}  # kind -> row ids answering HTTP 403
        self.outage = 0
        self.listed = False

    # -- 1. List ---------------------------------------------------------------
    def list(self) -> None:
        selected = self.selected
        # Products are swept whenever collections or orders need their titles.
        need_products = {"products", "collections", "orders"} & set(selected)
        products = _list_all(self.client, _PRODUCT_SWEEP, "products") if need_products else []
        self.titles = {p["id"]: _text(p.get("title")) for p in products}
        collections = (
            _list_all(self.client, _COLLECTION_SWEEP, "collections")
            if "collections" in selected
            else []
        )
        customers = _customer_sweep(self.client) if "customers" in selected else []
        self.names = {c["id"]: _text(c.get("displayName")) for c in customers}
        destroyed, self.event_cursor = _destroyed(self.client, self.event_cursor)

        swept: dict[str, list[dict]] = {
            "product": products if "products" in selected else [],
            "collection": collections,
            "customer": customers,
        }
        for kind, items in swept.items():
            for item in items:
                self.listing[_row_id(kind, item["id"])] = _text(item.get("updatedAt"))
        self.current_ids = set(self.listing)
        swept_count = len(self.listing)
        kinds_swept = {"product": "products", "collection": "collections", "customer": "customers"}
        missing = sorted(
            row_id
            for row_id in self.known_ids - self.current_ids
            if kinds_swept.get(row_id.split(":", 1)[0]) in selected
        )
        all_destroyed = set().union(*destroyed.values()) if destroyed else set()
        self.current_ids |= set(missing) - self._confirm_gone(missing, all_destroyed)

        renamed: list[str] = []
        if "orders" in selected:
            renamed = self._list_orders(destroyed.get("order", set()))

        work: list[tuple[str, str, str]] = []
        for kind in ("product", "customer", "collection"):
            for item in swept[kind]:
                row_id = _row_id(kind, item["id"])
                unchanged = self.stamps.get(row_id) == item.get("updatedAt")
                if kind != "collection" and unchanged and row_id in self.known_ids:
                    continue
                work.append((kind, row_id, item["id"]))
        work += [("order", row_id, _gid(*row_id.split(":", 1))) for row_id in renamed]
        # A forgotten unreadable item is asked again only once its stamp moves.
        for row_id, stamp in list(self.given_up.items()):
            if row_id not in self.listing:
                del self.given_up[row_id]
            elif self.listing[row_id] == stamp:
                self.current_ids.discard(row_id)
            else:
                del self.given_up[row_id]
        work = [w for w in work if w[1] not in self.given_up]
        self.retry &= {row_id for _, row_id, _ in work}
        self.work = [w for w in work if w[1] not in self.retry] + [
            w for w in work if w[1] in self.retry
        ]
        self.stats["scanned"] = swept_count + len(self.order_rows)
        self.listed = True

    def _list_orders(self, destroyed: set[str]) -> list[str]:
        """List changed orders, find deleted ones, and return the unlisted ones to re-fetch."""
        self.now = self.now or _shop_now(self.client)
        query = None
        if self.order_cursor:
            since = datetime.fromisoformat(self.order_cursor) - _ORDER_OVERLAP
            query = f"updated_at:>='{_stamp(since)}'"
        self.order_rows = _list_all(self.client, _ORDERS, "orders", {"query": query})
        listed_ids = {_row_id("order", order["id"]) for order in self.order_rows}
        # Orders are never swept: a known one stays current until confirmed deleted.
        self.current_ids |= {row_id for row_id in self.known_ids if row_id.startswith("order:")}
        self.current_ids |= listed_ids
        self.current_ids -= destroyed
        # Deleting an order leaves no destroy event (seen live), so each known
        # order not listed this run is asked about by id while it is at least
        # _ORDER_CHECK_MARGIN inside the window; a null answer means it was
        # deleted. Older orders are kept: null could mean they aged out.
        checkable = _stamp(self.now - _ORDER_WINDOW + _ORDER_CHECK_MARGIN)
        unlisted = sorted(
            row_id
            for row_id, refs in self.order_refs.items()
            if row_id in self.current_ids
            and row_id not in listed_ids
            and refs.get("created", "") >= checkable
        )
        deleted_orders, _ = self._null_nodes(unlisted)
        self.current_ids -= deleted_orders
        # An order shows its customer and products by name. One that was not
        # listed this run (unchanged) is fetched again when a name it shows
        # changed: its stored references are checked against this run's sweeps.
        renamed = []
        for row_id, refs in sorted(self.order_refs.items()):
            names_now = _names_hash(refs.get("ids") or [], self.names, self.titles)
            stale = refs.get("names") != names_now
            if row_id in self.current_ids and row_id not in listed_ids and stale:
                self.listing[row_id] = names_now
                renamed.append(row_id)
        return renamed

    def _confirm_gone(self, missing: list[str], destroyed: set[str]) -> set[str]:
        """Which ``missing`` row ids are confirmed gone: a destroy event or a null node.

        One that still resolves is kept and asked about again next run; after
        ``_GIVE_UP_RUNS`` runs in a row it is forgotten (it left the sweep).
        """
        gone = {row_id for row_id in missing if row_id in destroyed}
        null, resolving = self._null_nodes([row_id for row_id in missing if row_id not in gone])
        for row_id in resolving:
            self.ambiguous.add(row_id)
            self._count_unreadable(row_id, "missing")
            logger.warning(
                "Shopify: kept %s for a retry next run (missing from the listing but "
                "still resolving).",
                row_id,
            )
        return gone | null

    def _null_nodes(self, row_ids: list[str]) -> tuple[set[str], list[str]]:
        """Ask ``nodes(ids:)`` about ``row_ids``: those answering null, and those resolving.

        A batch that fails is kept (in neither result) and asked about next run.
        """
        null: set[str] = set()
        resolving: list[str] = []
        for start in range(0, len(row_ids), _NODES_BATCH):
            batch = row_ids[start : start + _NODES_BATCH]
            gids = [_gid(*row_id.split(":", 1)) for row_id in batch]
            try:
                data, _ = _graphql(self.client, _NODES, {"ids": gids})
            except ShopifyAPIError as exc:
                self.stats["failed"] += 1
                self.stop_if_systemic(exc)
                _raise_if_access_denied(exc)
                if exc.status == 401:
                    raise
                logger.warning("Shopify: could not check %d id(s) (%s).", len(batch), exc)
                continue
            nodes = data.get("nodes")
            if not isinstance(nodes, list) or len(nodes) != len(gids):
                raise ShopifyResponseError("nodes(ids:) did not answer one entry per id")
            for row_id, node in zip(batch, nodes, strict=True):
                if node is None:
                    null.add(row_id)
                else:
                    resolving.append(row_id)
        return null, resolving

    # -- 2. Render -------------------------------------------------------------
    def render(self) -> Iterator[dict[str, Any]]:
        for index, (kind, row_id, gid) in enumerate(self.work):
            self.position = index
            try:
                row = self._fetch(kind, row_id, gid)
            except ShopifyAPIError as exc:
                self._failed(kind, row_id, exc)
                continue
            if row_id not in self.retry:
                self.outage = 0
            self.unreadable.pop(row_id, None)
            self.fetched_kinds.add(kind)
            if row is not None:
                self.rendered.add(row_id)
                yield row
        self.position = len(self.work)
        if "orders" in self.selected:
            yield from self._order_rows()

    def _fetch(self, kind: str, row_id: str, gid: str) -> dict[str, Any] | None:
        """Fetch one item in full and return its row (``None``: nothing to emit)."""
        if kind in ("product", "customer"):
            query = _PRODUCT if kind == "product" else _CUSTOMER
            full, fetched_at = _single(self.client, query, kind, gid)
            settled = _is_settled(_text(full.get("updatedAt")), fetched_at)
            self.stamps[row_id] = _text(full.get("updatedAt")) if settled else ""
            if kind == "product":
                url, content = _text(full.get("onlineStoreUrl")), _render_product(full)
                return _row(row_id, _text(full.get("title")), url, content)
            return _row(row_id, _text(full.get("displayName")), "", _render_customer(full))
        if kind == "collection":
            # Read every run: its text shows its members by title, and neither a
            # membership change nor a product rename shows in the listing.
            full, _ = _single(self.client, _COLLECTION, "collection", gid)
            content = _render_collection(full, self.titles)
            if self.texts.get(row_id) == _hash(content) and row_id in self.known_ids:
                return None
            self.texts[row_id] = _hash(content)
            return _row(row_id, _text(full.get("title")), "", content)
        self.refetched[row_id], _ = _single(self.client, _ORDER, "order", gid)
        return None

    def _order_rows(self) -> Iterator[dict[str, Any]]:
        listed = {_row_id("order", order["id"]): order for order in self.order_rows}
        listed.update(self.refetched)
        for row_id, order in sorted(listed.items()):
            if row_id not in self.current_ids:
                continue  # given up after being unreadable
            content = _render_order(order, self.names, self.titles)
            ids = _flat_refs(order)
            self.order_refs[row_id] = {
                "created": _text(order.get("createdAt")),
                "ids": ids,
                "names": _names_hash(ids, self.names, self.titles),
            }
            if self.texts.get(row_id) == _hash(content) and row_id in self.known_ids:
                continue
            self.texts[row_id] = _hash(content)
            self.rendered.add(row_id)
            yield _row(row_id, _text(order.get("name")), "", content)
        if self.order_rows:
            stamps = (_text(o.get("updatedAt")) for o in self.order_rows)
            self.order_cursor = max([self.order_cursor, *stamps])
        # References are kept only while the order can still be read again.
        oldest = _stamp(self.now - _ORDER_WINDOW)
        self.order_refs = {
            k: v for k, v in self.order_refs.items() if v.get("created", "") >= oldest
        }

    def _failed(self, kind: str, row_id: str, exc: ShopifyAPIError) -> None:
        """Keep an item that failed on its own, or stop if Shopify as a whole is failing."""
        self.stats["failed"] += 1
        self.stop_if_systemic(exc)
        _raise_if_access_denied(exc)
        if exc.status == 401:
            raise exc  # nothing rendered yet: the credentials are bad
        self.kept.add(row_id)
        if kind in ("product", "customer"):
            self.stamps[row_id] = ""  # the old document stays, and is fetched next run
        elif kind == "collection":
            self.texts[row_id] = ""
        logger.warning("Shopify: kept %s for a retry next run (%s).", row_id, exc)
        if exc.status == 403:
            # Counted only once the run shows others of its kind can be read (_settle_403s).
            self.forbidden.setdefault(kind, []).append(row_id)
        elif _is_unreadable(exc):
            self._count_unreadable(row_id, self.listing.get(row_id, ""))
        elif row_id not in self.retry and (exc.status == 0 or exc.status in _TRANSIENT_STATUSES):
            self.outage += 1
            if self.outage >= _OUTAGE_STREAK:
                raise _CutShortError("unavailable")

    def _count_unreadable(self, row_id: str, stamp: str) -> None:
        entry = self.unreadable.get(row_id) or {}
        runs = entry.get("runs", 0) + 1 if entry.get("stamp") == stamp else 1
        self.unreadable[row_id] = {"stamp": stamp, "runs": runs}

    def _settle_403s(self) -> None:
        """Count a 403 toward give-up only if another item of its kind was read this run.

        When every fetch of a kind answered 403, the token lost a permission: nothing
        moves toward forgetting, and one warning per kind says so.
        """
        for kind, row_ids in sorted(self.forbidden.items()):
            if kind in self.fetched_kinds:
                for row_id in row_ids:
                    self._count_unreadable(row_id, self.listing.get(row_id, ""))
                continue
            self.stats["failed_permission"] = self.stats.get("failed_permission", 0) + len(row_ids)
            logger.warning(
                "Shopify: %s: every fetch answered 403 this run; check the token's permissions.",
                kind,
            )
        self.forbidden = {}

    def stop_if_systemic(self, exc: ShopifyAPIError) -> None:
        if exc.rate_limited:
            raise _CutShortError("rate_limit") from None
        if exc.status == 401 and self.rendered:
            raise _CutShortError("auth") from None

    # -- 3. Delete and 4. Commit ------------------------------------------------
    def finish(self) -> Iterator[dict[str, Any]]:
        self._settle_403s()
        for row_id, entry in self.unreadable.items():
            if row_id in self.kept | self.ambiguous and entry["runs"] >= _GIVE_UP_RUNS:
                logger.warning(
                    "Shopify: %s stayed unreadable for %d runs; its document is forgotten "
                    "until it changes.",
                    row_id,
                    entry["runs"],
                )
                if row_id in self.listing and entry["stamp"] != "missing":
                    self.given_up[row_id] = entry["stamp"]
                self.kept.discard(row_id)
                self.ambiguous.discard(row_id)
                self.current_ids.discard(row_id)
                self.stats["given_up"] += 1
        deleted = sorted(self.known_ids - self.current_ids)
        for row_id in deleted:
            yield _deleted_row(row_id)
        self.stats["deleted"] = len(deleted)
        self._commit(self.current_ids, self.kept)
        self.state["event_cursor"] = self.event_cursor
        self.state["order_cursor"] = self.order_cursor

    def commit_progress(self) -> None:
        """After a clean stop: keep what was rendered, delete nothing, move no cursor.

        Products and customers not reached keep their old stamp and collections
        are read every run, so the next run finds them again; orders and destroy
        events are listed again from the unchanged cursors.
        """
        if not self.listed:
            return  # stopped while listing: nothing moved
        self._settle_403s()
        rest = {row_id for _, row_id, _ in self.work[self.position :]}
        self._commit(self.known_ids | self.rendered, self.kept | (self.retry & rest), rest)

    def _commit(self, current_ids: set[str], retry: set[str], rest: Iterable[str] = ()) -> None:
        unsettled = retry | self.ambiguous | set(rest)
        self.state["known_ids"] = sorted(current_ids)
        self.state["stamps"] = {k: v for k, v in sorted(self.stamps.items()) if k in current_ids}
        self.state["texts"] = {k: v for k, v in sorted(self.texts.items()) if k in current_ids}
        self.state["orders"] = {
            k: v for k, v in sorted(self.order_refs.items()) if k in current_ids
        }
        self.state["retry"] = sorted(retry)
        self.state["unreadable"] = {
            k: v for k, v in sorted(self.unreadable.items()) if k in unsettled
        }
        self.state["given_up"] = dict(sorted(self.given_up.items()))
        self.stats["kept"] = len(retry | self.ambiguous)
        failed_items = len(self.kept)
        self.stats["skipped"] = max(
            0, self.stats["scanned"] - len(self.rendered) - failed_items - len(self.given_up)
        )


def sync_store(
    client: _Client,
    state: dict,
    *,
    resources: Iterable[str] = DEFAULT_RESOURCES,
    now: datetime | None = None,
    stats: dict[str, int] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed documents since the last run, plus hard-delete markers.

    1. *List.* The sweeps, the destroy events and, for orders, the change
       listing. Missing ids are confirmed gone or kept. A failure here
       propagates before a single row.
    2. *Render.* Each changed item (fresh first, then retries), then the orders.
    3. *Delete.* A hard-delete marker for every confirmed deletion.
    4. *Commit.* Only now is ``state`` written. ``now`` (for tests) defaults to
       Shopify's clock, read from the run's first answer.

    A run stopped cleanly (a ``401`` after progress, a rate limit, an outage)
    keeps what it rendered and deletes nothing; the next run picks up the rest.
    """
    selected = [r for r in RESOURCES if r in set(resources)]
    stats = {} if stats is None else stats
    for key in _STAT_KEYS:
        stats.setdefault(key, 0)
    run = _Run(client, state, stats, selected, now)
    try:
        try:
            run.list()
            yield from run.render()
            yield from run.finish()
        except ShopifyAPIError as exc:
            run.stop_if_systemic(exc)  # also a rate limit while listing
            _raise_if_access_denied(exc)
            raise
    except _CutShortError as cut:
        run.commit_progress()
        stats["failed"] = max(1, stats["failed"])
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Shopify sync stopped early (%s); it resumes on the next run.", cut.reason)
        return
    logger.info(
        "Shopify: %d document(s) rendered, %d deletion(s) across %s; kept %d item(s) for retry.",
        len(run.rendered),
        stats["deleted"],
        ", ".join(selected),
        stats["kept"],
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
            exc.endpoint,
            exc.detail,
            code=exc.code,
            hint="the app may not read customers or their name and email (protected customer "
            "data); in the Dev Dashboard give the app customer access with the Name and Email "
            "fields, or leave 'customers' out of resources",
        ) from None


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
    service: Any = None,
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
        resource_name: dlt resource (table) name, which also scopes the sync
            state. Give each store its own name in a shared dataset.
        check_active: Optional host authorization checkpoint, called before every
            request and wait and around every row; it stops the sync by raising.
        service: Pre-built ``requests``-like session, mainly for tests; when
            omitted one is built for each run and closed after it. ``session``
            is accepted as an alias.

    Returns:
        A ``dlt`` resource with ``primary_key="id"``, ``write_disposition="merge"``
        and a ``_deleted`` hard-delete column. ``resource.cognee_sync_stats``
        holds the last run's counts.
    """
    if not (hasattr(dlt_utils, "PIPELINE_SCOPE_ATTR") and hasattr(dlt_utils, "guarded_rows")):
        raise RuntimeError(
            "The Shopify connector needs cognee's dlt_utils.PIPELINE_SCOPE_ATTR and "
            "guarded_rows (cognee>=1.6.2)."
        )
    _check_resource_name(resource_name)
    if service is not None and session is not None:
        raise ValueError("Pass service= or session=, not both")
    service = service if service is not None else session

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
    for value, what in ((token, "access token"), (cid, "client id"), (secret, "client secret")):
        if value:
            _check_secret(value, what)
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the destination on merge,
        # which propagates the deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def shopify_documents():
        stats.clear()
        http = service if service is not None else _make_session()
        try:
            client = _Client(
                http,
                resolved_shop,
                token,
                None if token else cid,
                None if token else secret,
                check_active,
            )
            rows = sync_store(client, dlt.current.resource_state(), resources=chosen, stats=stats)
            yield from dlt_utils.guarded_rows(rows, check_active)
        finally:
            if service is None:
                http.close()

    resource = shopify_documents()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(resource, DOCUMENT_SOURCE_ATTR, SHOPIFY_SOURCE_NAME)
    # Give the sync state its own dlt pipeline, apart from other sources.
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource


def _check_resource_name(name: Any) -> None:
    """A non-empty name of at most 64 characters that dlt keeps as it is.

    It names the table and scopes the state: an empty name would lose that
    scope, and two names dlt normalizes alike ("Foo-Bar", "foo_bar") would share
    a table.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty string")
    if len(name) > 64:
        raise ValueError("resource_name must be at most 64 characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")
