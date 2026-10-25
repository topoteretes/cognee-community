"""WooCommerce connector for cognee: a ``dlt`` source that turns a store into memory.

One document per product, approved product review and order (with its notes),
and optionally per customer, synced incrementally with forget-on-delete. Hand
the source to :func:`cognee.remember`::

    await cognee.remember(
        woocommerce_source(),  # WOOCOMMERCE_STORE_URL / _CONSUMER_KEY / _CONSUMER_SECRET
        dataset_name="store",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

The README describes what is stored, how changes and deletions are found, and
what a run does when the store fails part of the way through.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlsplit

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

logger = get_logger("woocommerce_connector")

# Default dlt resource / staging-table name, and the document-source tag for its rows.
WOOCOMMERCE_TABLE_NAME = "woocommerce_documents"
WOOCOMMERCE_SOURCE_NAME = "woocommerce"

# Everything the connector can ingest, and what it ingests unless told otherwise.
# Customers are opt-in: they are the one resource that is personal data first.
RESOURCES = ("products", "reviews", "orders", "customers")
DEFAULT_RESOURCES = ("products", "reviews", "orders")

_API_PATH = "/wp-json/wc/v3"
# Per request: a generous bound for one API call, so a stalled connection fails and is retried.
_TIMEOUT_SECONDS = 30
_PAGE_SIZE = 100  # WooCommerce caps ``per_page`` at 100 for every collection
# Five attempts per request, with 1, 2, 4, 8 s between them, or the store's
# Retry-After on a 429.
_MAX_RETRIES = 5
# 429 comes from host-level rate limiting (WooCommerce has none of its own);
# the 5xx answers are the usual temporary failures of a PHP host.
_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
# The longest single wait, and the most a run waits in all. cognee holds its dlt
# staging lock during a sync, so a longer Retry-After, or a wait past the
# budget, ends the run instead (the next run starts over from the saved state).
_MAX_WAIT_SECONDS = 60
_MAX_TOTAL_WAIT_SECONDS = 120
# Pages per listing before it is treated as broken: 10,000 pages of 100 is far
# beyond any store, and stops a listing whose page count keeps growing.
_MAX_LIST_PAGES = 10_000
# How often a listing is read again from page one after the store's total moved
# while it was paged (see _list). One re-read normally suffices; two leave slack.
_MAX_LISTING_RESTARTS = 2
# How far before the cursor the modified_after window starts. ``modified_after``
# is exclusive and a record saved in the same second as the cursor may not have
# been visible to the previous run yet; a minute is a wide margin, and the
# fingerprints keep the overlap from re-ingesting anything.
_CURSOR_OVERLAP = timedelta(seconds=60)
# Records that fail to load (5xx or no answer, after retries) in a row before the
# run is treated as an outage and stopped. One or two can be a single broken
# order; three different records back to back rarely are.
_OUTAGE_STREAK = 3

_STAT_KEYS = ("listed", "rendered", "unchanged", "deleted", "kept", "failed")


class WooCommerceAPIError(RuntimeError):
    """An error answer from the store (``status``), or no answer at all (``status`` 0).

    The message names only the status and the endpoint, never a record id, a
    key or a response body. WooCommerce's own error code is on ``detail``.
    """

    def __init__(self, status: int, endpoint: str, detail: str = ""):
        answer = f"HTTP {status}" if status else "no answer"
        super().__init__(f"WooCommerce {endpoint}: {answer}")
        self.status = status
        self.endpoint = endpoint
        self.detail = detail


class WooCommerceResponseError(RuntimeError):
    """The store answered ``200`` with a body that is not of the expected shape.

    Never read as an empty listing: it aborts the run before anything is deleted.
    """


# ---------------------------------------------------------------------------
# Resource kinds
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Kind:
    name: str  # selection name, state key and node set
    prefix: str  # document id prefix
    list_path: str
    item_path: str  # with ``{id}``
    incremental: bool  # lists by ``modified_after``; otherwise re-listed in full


_KINDS = {
    "products": _Kind("products", "product", "/products", "/products/{id}", True),
    "reviews": _Kind("reviews", "review", "/products/reviews", "/products/reviews/{id}", False),
    "orders": _Kind("orders", "order", "/orders", "/orders/{id}", True),
    "customers": _Kind("customers", "customer", "/customers", "/customers/{id}", False),
}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _check_credential(value: str, name: str) -> None:
    """Refuse a key ``requests`` would reject, without ever echoing it.

    A pasted key often ends in a newline; requests then raises an error whose
    message holds the whole header or URL, key included.
    """
    if any(char.isspace() or not char.isprintable() for char in value):
        raise ValueError(f"The WooCommerce {name} contains whitespace or control characters")


def _normalize_store_url(store_url: str) -> str:
    """``https://shop.example.com`` (no trailing slash), or ``ValueError``."""
    if not isinstance(store_url, str) or not store_url.strip():
        raise ValueError("store_url must be the store's address, e.g. https://shop.example.com")
    parts = urlsplit(store_url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("store_url must start with https:// (or http:// for a local store)")
    if parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError("store_url must not carry a query, a fragment or credentials")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def _rfc3986(value: str) -> str:
    """Percent-encode as OAuth 1.0a (RFC 5849, section 3.6) and WooCommerce expect."""
    return quote(value, safe="~")


def oauth1_params(
    url: str,
    params: dict[str, Any],
    consumer_key: str,
    consumer_secret: str,
    *,
    nonce: str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    """``params`` plus one-legged OAuth 1.0a query parameters for a GET of ``url``.

    WooCommerce only accepts consumer keys over plain HTTP this way: no token,
    the oauth_* fields in the query string, signed with ``consumer_secret + "&"``
    (``WC_REST_Authentication::check_oauth_signature``). HMAC-SHA256 is used; the
    store also accepts HMAC-SHA1.
    """
    signed = {key: str(value) for key, value in params.items()}
    signed.update(
        {
            "oauth_consumer_key": consumer_key,
            "oauth_nonce": nonce or secrets.token_hex(16),
            "oauth_signature_method": "HMAC-SHA256",
            "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
        }
    )
    pairs = sorted((_rfc3986(key), _rfc3986(value)) for key, value in signed.items())
    normalized = "&".join(f"{key}={value}" for key, value in pairs)
    base_string = "&".join(("GET", _rfc3986(url), _rfc3986(normalized)))
    digest = hmac.new(f"{consumer_secret}&".encode(), base_string.encode(), hashlib.sha256).digest()
    signed["oauth_signature"] = base64.b64encode(digest).decode()
    return signed


class _Client:
    """One run's access to the store: the session, the credentials, the waits."""

    def __init__(
        self,
        session: Any,
        store_url: str,
        consumer_key: str,
        consumer_secret: str,
        check_active: Callable[[], None] | None = None,
    ):
        self.session = session
        self.api_base = f"{store_url}{_API_PATH}"
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        # WooCommerce reads consumer keys from basic auth over HTTPS only; plain
        # HTTP needs OAuth 1.0a signatures instead.
        self.oauth = urlsplit(store_url).scheme == "http"
        self.check_active = check_active
        self.waited = 0.0  # seconds slept this run
        self.requests = 0

    def check(self) -> None:
        if self.check_active is not None:
            self.check_active()

    def can_wait(self, seconds: float) -> bool:
        """Whether a wait fits in the single-wait cap and the run's budget."""
        return seconds <= _MAX_WAIT_SECONDS and self.waited + seconds <= _MAX_TOTAL_WAIT_SECONDS

    def wait(self, seconds: float) -> None:
        self.check()
        self.waited += seconds
        time.sleep(seconds)

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        endpoint: str,
        ok: tuple[int, ...] = (200,),
    ) -> tuple[int, Any, Any]:
        """GET an API path and return ``(status, json, headers)``, retrying transient errors.

        ``endpoint`` is the label errors and logs use (``/orders/{id}``), so a
        record id never ends up in a message. Rate limits (429), server errors
        (5xx) and network failures are retried up to ``_MAX_RETRIES`` times; a
        wait over ``_MAX_WAIT_SECONDS``, or past the run's
        ``_MAX_TOTAL_WAIT_SECONDS``, is not taken. Every other status outside
        ``ok`` raises :class:`WooCommerceAPIError`.
        """
        url = f"{self.api_base}{path}"
        attempt = 0
        while True:
            last_attempt = attempt == _MAX_RETRIES - 1
            self.check()
            self.requests += 1
            try:
                response = self._send(url, params or {})
            except requests.RequestException as exc:
                # Only the type: the message of some requests errors holds the
                # URL, and over HTTP the URL carries the signed consumer key.
                delay = float(2**attempt)
                if last_attempt or not self.can_wait(delay):
                    raise WooCommerceAPIError(0, endpoint, type(exc).__name__) from None
                logger.warning(
                    "WooCommerce: %s on %s; retrying in %.0fs.", type(exc).__name__, endpoint, delay
                )
                self.wait(delay)
                attempt += 1
                continue

            status = response.status_code
            if status in ok:
                return status, _json_body(response, endpoint), response.headers
            if status in _TRANSIENT_STATUSES and not last_attempt:
                delay = _retry_delay(response.headers, attempt)
                if not self.can_wait(delay):
                    raise WooCommerceAPIError(status, endpoint, f"asked to wait {delay:.0f}s")
                logger.warning(
                    "WooCommerce: HTTP %d on %s; retrying in %.0fs.", status, endpoint, delay
                )
                self.wait(delay)
                attempt += 1
                continue
            raise WooCommerceAPIError(status, endpoint, _error_code(response))

    def _send(self, url: str, params: dict[str, Any]) -> Any:
        if self.oauth:
            # A fresh nonce and timestamp on every attempt: the store refuses a
            # nonce it has seen in the last 15 minutes.
            signed = oauth1_params(url, params, self.consumer_key, self.consumer_secret)
            return self.session.get(url, params=signed, timeout=_TIMEOUT_SECONDS)
        return self.session.get(
            url,
            params=params,
            auth=(self.consumer_key, self.consumer_secret),
            timeout=_TIMEOUT_SECONDS,
        )


def _json_body(response: Any, endpoint: str) -> Any:
    try:
        return response.json()
    except ValueError:
        # An HTML page (a login wall, a host's error page) answered with 200.
        raise WooCommerceResponseError(
            f"WooCommerce {endpoint}: HTTP {response.status_code} without JSON"
        ) from None


def _retry_delay(headers: Any, attempt: int) -> float:
    """Seconds to wait before a retry: ``Retry-After`` when the store sends one."""
    try:
        return max(0.0, float(_header(headers, "Retry-After")))
    except (TypeError, ValueError):
        return float(2**attempt)


def _error_code(response: Any) -> str:
    """WooCommerce's error ``code`` (``woocommerce_rest_cannot_view``), or nothing.

    Never the message or the raw body: either can echo request data back.
    """
    try:
        code = response.json().get("code")
    except (ValueError, AttributeError):
        return ""
    return code[:100] if isinstance(code, str) else ""


def _header(headers: Any, name: str) -> Any:
    """A response header, case-insensitively, from a mapping or ``requests`` headers."""
    if not headers:
        return None
    if name in headers:
        return headers[name]
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


class _ListingShiftedError(Exception):
    """Internal: the collection's size changed between two pages of one listing."""


def _list(client: _Client, path: str, params: dict[str, Any], *, endpoint: str) -> list[dict]:
    """Every record of a paginated collection, in order. Never a partial list.

    Paging follows ``X-WP-TotalPages``; a page without it, a body that is not a
    list, or an entry that is not an object with an ``id`` raises, so a broken
    page can never pass for a short (and deletion-triggering) listing.

    WooCommerce pages by offset, so a record deleted (or added) while a listing
    is paged shifts every later record by one, and one of them would be skipped:
    a skipped record on the modified listing would be passed by the cursor and
    missed until it changes again. ``X-WP-Total`` is compared on every page, and
    a listing whose total moved is read again from the first page.
    """
    for _ in range(_MAX_LISTING_RESTARTS + 1):
        try:
            return _list_pages(client, path, params, endpoint=endpoint)
        except _ListingShiftedError:
            logger.warning("WooCommerce: %s changed while it was paged; listing again.", endpoint)
    raise WooCommerceResponseError(
        f"WooCommerce {endpoint}: the listing kept changing while it was paged"
    )


def _list_pages(client: _Client, path: str, params: dict[str, Any], *, endpoint: str) -> list[dict]:
    records: dict[str, dict] = {}
    first_total = None
    page = 1
    while True:
        if page > _MAX_LIST_PAGES:
            raise WooCommerceResponseError(
                f"WooCommerce {endpoint}: more than {_MAX_LIST_PAGES} pages"
            )
        _, body, headers = client.get(
            path, {**params, "per_page": _PAGE_SIZE, "page": page}, endpoint=endpoint
        )
        if not isinstance(body, list):
            raise WooCommerceResponseError(
                f"WooCommerce {endpoint}: a {type(body).__name__}, not a list"
            )
        for record in body:
            if not isinstance(record, dict) or record.get("id") in (None, ""):
                raise WooCommerceResponseError(f"WooCommerce {endpoint}: an entry without an id")
            # A record edited while the listing is paged by modified date moves
            # to a later page and is seen twice; the later copy is the newer one.
            records.pop(str(record["id"]), None)
            records[str(record["id"])] = record
        total_pages = _total_pages(headers, endpoint)
        total = _header(headers, "X-WP-Total")
        if page == 1:
            first_total = total
        elif total != first_total:
            raise _ListingShiftedError
        if page >= total_pages:
            return list(records.values())
        page += 1


def _total_pages(headers: Any, endpoint: str) -> int:
    value = _header(headers, "X-WP-TotalPages")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise WooCommerceResponseError(
            f"WooCommerce {endpoint}: no X-WP-TotalPages header on a listing"
        ) from None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}


class _TextExtractor(HTMLParser):
    """Plain text from the HTML WooCommerce stores in descriptions and reviews."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def html_to_text(value: Any) -> str:
    """Readable text from a WooCommerce HTML field (tags dropped, entities decoded)."""
    if not isinstance(value, str) or not value.strip():
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    lines = (
        re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(parser.parts).split("\n")
    )
    text = "\n".join(line for line in lines if line and line != "-")
    return text.strip()


def _names(items: Any, key: str = "name") -> list[str]:
    if not isinstance(items, list):
        return []
    return [str(item[key]) for item in items if isinstance(item, dict) and item.get(key)]


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _attribute_lines(attributes: Any) -> list[str]:
    lines = []
    for attribute in attributes if isinstance(attributes, list) else []:
        if not isinstance(attribute, dict) or not attribute.get("name"):
            continue
        options = attribute.get("options")
        values = ", ".join(str(o) for o in options) if isinstance(options, list) else ""
        option = attribute.get("option")  # a variation's attribute holds one value
        lines.append(f"- {attribute['name']}: {values or _text(option)}")
    return lines


def _price_line(record: dict) -> str:
    regular = _text(record.get("regular_price"))
    sale = _text(record.get("sale_price"))
    if regular and sale:
        return f"Price: {sale} (regular {regular})"
    price = regular or _text(record.get("price"))
    return f"Price: {price}" if price else ""


def render_product(product: dict, variations: list[dict] | None = None) -> dict[str, Any]:
    """A product as a document.

    Stock levels, sales counts, ratings and modification times are left out on
    purpose: they change all the time, and every change would re-cognify the
    product.
    """
    name = _text(product.get("name")) or f"Product {product['id']}"
    lines = [f"Product: {name}"]
    for label, value in (
        ("SKU", _text(product.get("sku"))),
        ("Type", _text(product.get("type"))),
        ("Categories", ", ".join(_names(product.get("categories")))),
        ("Tags", ", ".join(_names(product.get("tags")))),
    ):
        if value:
            lines.append(f"{label}: {value}")
    price = _price_line(product)
    if price:
        lines.append(price)
    attributes = _attribute_lines(product.get("attributes"))
    if attributes:
        lines += ["Attributes:", *attributes]
    for label, field in (("Summary", "short_description"), ("Description", "description")):
        text = html_to_text(product.get(field))
        if text:
            lines += ["", f"{label}:", text]
    if variations:
        lines += ["", "Variations:"]
        for variation in sorted(variations, key=lambda v: int(v.get("id") or 0)):
            options = "; ".join(
                f"{a.get('name')}: {a.get('option')}"
                for a in variation.get("attributes") or []
                if isinstance(a, dict) and a.get("name")
            )
            details = [options or f"variation {variation.get('id')}", _price_line(variation)]
            sku = _text(variation.get("sku"))
            if sku:
                details.append(f"SKU {sku}")
            lines.append("- " + " — ".join(part for part in details if part))
    return _row("product", product["id"], f"Product: {name}", lines, product.get("permalink"))


def render_review(review: dict, *, include_reviewer_names: bool = False) -> dict[str, Any]:
    """An approved product review as a document. The reviewer's email is never stored."""
    product = _text(review.get("product_name")) or f"product {review.get('product_id')}"
    lines = [f"Review of {product}"]
    rating = review.get("rating")
    if isinstance(rating, int) and rating > 0:
        lines.append(f"Rating: {rating}/5")
    if review.get("verified") is True:
        lines.append("Verified owner: yes")
    if include_reviewer_names and _text(review.get("reviewer")):
        lines.append(f"Reviewer: {_text(review.get('reviewer'))}")
    created = _text(review.get("date_created_gmt"))
    if created:
        lines.append(f"Written: {created} UTC")
    text = html_to_text(review.get("review"))
    if text:
        lines += ["", text]
    return _row(
        "review", review["id"], f"Review of {product}", lines, review.get("product_permalink")
    )


def render_order(order: dict, notes: list[dict]) -> dict[str, Any]:
    """An order as a document: what was bought, its state, and the notes on it.

    Billing and shipping addresses, emails and phone numbers are never stored;
    the customer is referenced by id only.
    """
    number = _text(order.get("number")) or str(order["id"])
    lines = [f"Order #{number}"]
    for label, value in (
        ("Status", _text(order.get("status"))),
        ("Placed", f"{order['date_created_gmt']} UTC" if order.get("date_created_gmt") else ""),
        ("Customer", f"customer #{order['customer_id']}" if order.get("customer_id") else "guest"),
        ("Payment method", _text(order.get("payment_method_title"))),
    ):
        if value:
            lines.append(f"{label}: {value}")
    currency = _text(order.get("currency"))
    total = _text(order.get("total"))
    if total:
        lines.append(f"Total: {total} {currency}".rstrip())
    for label, field in (("Discount", "discount_total"), ("Shipping cost", "shipping_total")):
        value = _text(order.get(field))
        if value and value not in ("0", "0.00"):
            lines.append(f"{label}: {value} {currency}".rstrip())
    shipping = ", ".join(_names(order.get("shipping_lines"), "method_title"))
    if shipping:
        lines.append(f"Shipping: {shipping}")
    coupons = ", ".join(_names(order.get("coupon_lines"), "code"))
    if coupons:
        lines.append(f"Coupons: {coupons}")
    items = [i for i in order.get("line_items") or [] if isinstance(i, dict)]
    if items:
        lines.append("Items:")
        for item in items:
            sku = f" (SKU {item['sku']})" if item.get("sku") else ""
            lines.append(
                f"- {item.get('quantity', 1)} x {_text(item.get('name'))}{sku}: "
                f"{_text(item.get('total'))} {currency}".rstrip()
            )
    customer_note = html_to_text(order.get("customer_note"))
    if customer_note:
        lines += ["", "Customer's note at checkout:", customer_note]
    rendered_notes = [
        f"- {note.get('date_created_gmt', '')} UTC"
        f" ({'to customer' if note.get('customer_note') else 'private'}): "
        f"{html_to_text(note.get('note'))}"
        for note in sorted(notes, key=lambda n: (str(n.get("date_created_gmt")), n.get("id", 0)))
        if html_to_text(note.get("note"))
    ]
    if rendered_notes:
        lines += ["", "Order notes:", *rendered_notes]
    return _row("order", order["id"], f"Order #{number}", lines, None)


def render_customer(customer: dict) -> dict[str, Any]:
    """A customer (opt-in) as a document: name, company and location, no contact data."""
    name = " ".join(
        part
        for part in (_text(customer.get("first_name")), _text(customer.get("last_name")))
        if part
    )
    name = name or f"Customer {customer['id']}"
    billing = customer.get("billing") if isinstance(customer.get("billing"), dict) else {}
    lines = [f"Customer: {name}", f"Customer id: {customer['id']}"]
    for label, value in (
        ("Company", _text(billing.get("company"))),
        (
            "Location",
            ", ".join(p for p in (_text(billing.get("city")), _text(billing.get("country"))) if p),
        ),
        (
            "Registered",
            f"{customer['date_created_gmt']} UTC" if customer.get("date_created_gmt") else "",
        ),
    ):
        if value:
            lines.append(f"{label}: {value}")
    return _row("customer", customer["id"], f"Customer: {name}", lines, None)


_KIND_BY_PREFIX = {kind.prefix: kind.name for kind in _KINDS.values()}


def _row(prefix: str, record_id: Any, title: str, lines: Iterable[str], url: Any) -> dict[str, Any]:
    """A document row. Only stable fields: every column feeds cognee's content hash."""
    return {
        "id": f"{prefix}:{record_id}",
        "title": title,
        "content": "\n".join(lines).strip(),
        "url": url if isinstance(url, str) and url else None,
        "_deleted": False,
        dlt_utils.NODE_SET_COLUMN: [_KIND_BY_PREFIX[prefix]],
    }


def _deleted_row(row_id: str) -> dict[str, Any]:
    return {"id": row_id, "_deleted": True}


def _fingerprint(row: dict[str, Any]) -> str:
    text = "\x1f".join(str(row.get(field) or "") for field in ("title", "content", "url"))
    return hashlib.sha256(text.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
@dataclass
class _Options:
    product_status: str = "publish"
    include_variations: bool = False
    include_private_notes: bool = False
    include_reviewer_names: bool = False
    order_notes_window_days: int = 7


def _parse_gmt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _gmt_stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _scope_params(kind: _Kind, options: _Options) -> dict[str, Any]:
    """The listing filter that defines which records of a kind are in scope."""
    if kind.name == "products":
        return {"status": options.product_status}
    if kind.name == "reviews":
        return {"status": "approved"}
    if kind.name == "orders":
        return {"status": "any"}  # every order status except trash
    return {"role": "customer"}  # store customers, not the site's staff accounts


# The fields a listing needs to tell whether an id is still in scope.
_SCOPE_FIELDS = {"products": "id,status", "reviews": "id,status", "orders": "id,status"}


def _in_scope(kind: _Kind, record: dict, options: _Options) -> bool:
    """Whether a listed or fetched record belongs to the selection."""
    status = record.get("status")
    if kind.name == "products":
        return status == options.product_status or (
            options.product_status == "any" and status != "trash"
        )
    if kind.name == "reviews":
        return status == "approved"
    if kind.name == "orders":
        # checkout-draft is the block checkout's cart in progress, not an order yet.
        return status not in ("trash", "checkout-draft")
    return record.get("role", "customer") == "customer"


class _Run:
    """One sync run over the selected kinds. State is written by :meth:`commit` only."""

    def __init__(
        self,
        client: _Client,
        state: dict,
        kinds: list[_Kind],
        options: _Options,
        stats: dict,
        now: datetime | None = None,
    ):
        self.client = client
        self.state = state
        self.kinds = kinds
        self.options = options
        self.stats = stats
        self.now = now or datetime.now(timezone.utc)
        self.plans: dict[str, dict[str, Any]] = {}

    # -- 1. list -------------------------------------------------------------
    def list_all(self) -> None:
        """List every selected kind and confirm deletions, before a single row."""
        for kind in self.kinds:
            saved = self.state.get(kind.name) or {}
            known: dict[str, str] = dict(saved.get("known") or {})
            retry = [str(i) for i in saved.get("retry") or []]
            scope = _scope_params(kind, self.options)
            cursor = saved.get("cursor")
            changed, cursor_after = self._list_changed(kind, scope, cursor)
            changed = [r for r in changed if _in_scope(kind, r, self.options)]
            if kind.incremental and cursor:
                fields = _SCOPE_FIELDS.get(kind.name, "id")
                current_ids = {
                    str(r["id"])
                    for r in _list(
                        self.client,
                        kind.list_path,
                        {**scope, "orderby": "id", "order": "asc", "_fields": fields},
                        endpoint=kind.list_path,
                    )
                    if _in_scope(kind, r, self.options)
                }
            else:
                current_ids = {str(r["id"]) for r in changed}
            self.stats["listed"] += len(current_ids)
            gone = self._confirm_gone(kind, sorted(set(known) - current_ids, key=_id_order))
            self.plans[kind.name] = {
                "known": known,
                "changed": changed,
                "retry": [i for i in retry if i in current_ids and i not in gone],
                "gone": gone,
                "cursor": cursor_after or cursor,
            }

    def _list_changed(
        self, kind: _Kind, scope: dict[str, Any], cursor: str | None
    ) -> tuple[list[dict], str | None]:
        if not kind.incremental:
            records = _list(self.client, kind.list_path, scope, endpoint=kind.list_path)
            return records, None
        params: dict[str, Any] = {
            **scope,
            "orderby": "modified",
            "order": "asc",
            "dates_are_gmt": "true",
        }
        since = _parse_gmt(cursor)
        if since is not None:
            start = since - _CURSOR_OVERLAP
            if kind.name == "orders" and self.options.order_notes_window_days > 0:
                # A note added to an order leaves its date_modified alone, so the
                # recently active orders are read again to pick up new notes.
                # Fingerprints keep the unchanged ones from being re-ingested.
                window = timedelta(days=self.options.order_notes_window_days)
                start = min(start, self.now - window)
            params["modified_after"] = _gmt_stamp(start)
        records = _list(self.client, kind.list_path, params, endpoint=kind.list_path)
        stamps = [s for s in (_parse_gmt(r.get("date_modified_gmt")) for r in records) if s]
        newest = max(stamps, default=None)
        if since is not None and newest is not None:
            newest = max(newest, since)
        return records, _gmt_stamp(newest) if newest else None

    def _confirm_gone(self, kind: _Kind, missing: list[str]) -> list[str]:
        """Of the ids that left the listing, the ones the store confirms are gone.

        A record counts as gone when it answers 404, or when it answers with a
        status outside the selection (trashed, unpublished, a review marked as
        spam). A record that still answers in scope stays. Any other failure
        aborts the run, so a flaky listing can never cause a deletion.
        """
        gone = []
        for record_id in missing:
            path = kind.item_path.format(id=record_id)
            status, body, _ = self.client.get(path, endpoint=kind.item_path, ok=(200, 404))
            if status == 404:
                gone.append(record_id)
            elif not isinstance(body, dict):
                raise WooCommerceResponseError(
                    f"WooCommerce {kind.item_path}: a {type(body).__name__}, not an object"
                )
            elif not _in_scope(kind, body, self.options):
                gone.append(record_id)
            else:
                logger.warning(
                    "WooCommerce: a %s missing from the listing is still in scope; kept.",
                    kind.prefix,
                )
        return gone

    # -- 2. render -----------------------------------------------------------
    def render(self) -> Iterator[dict[str, Any]]:
        for kind in self.kinds:
            plan = self.plans[kind.name]
            records = {str(r["id"]): r for r in plan["changed"]}
            kept: list[str] = []
            streak = 0
            ids = list(records) + [i for i in plan["retry"] if i not in records]
            for record_id in ids:
                try:
                    record = records.get(record_id) or self._fetch(kind, record_id)
                    if record is None:  # deleted since the listing; next run confirms it
                        continue
                    row = self._render(kind, record)
                except WooCommerceAPIError as exc:
                    if exc.status in (401, 403):
                        raise  # access is broken for everything, not this record
                    streak += 1
                    if streak >= _OUTAGE_STREAK:
                        raise
                    logger.warning(
                        "WooCommerce: a %s failed to load (%s); retried next run.",
                        kind.prefix,
                        exc,
                    )
                    kept.append(record_id)
                    self.stats["failed"] += 1
                    continue
                streak = 0
                fingerprint = _fingerprint(row)
                if plan["known"].get(record_id) == fingerprint:
                    self.stats["unchanged"] += 1
                    continue
                plan["known"][record_id] = fingerprint
                self.stats["rendered"] += 1
                yield row
            plan["kept"] = kept
            self.stats["kept"] += len(kept)

    def _fetch(self, kind: _Kind, record_id: str) -> dict | None:
        path = kind.item_path.format(id=record_id)
        status, body, _ = self.client.get(path, endpoint=kind.item_path, ok=(200, 404))
        if status == 404:
            return None
        if not isinstance(body, dict):
            raise WooCommerceResponseError(
                f"WooCommerce {kind.item_path}: a {type(body).__name__}, not an object"
            )
        return body if _in_scope(kind, body, self.options) else None

    def _render(self, kind: _Kind, record: dict) -> dict[str, Any]:
        if kind.name == "products":
            variations = None
            if self.options.include_variations and record.get("variations"):
                variations = _list(
                    self.client,
                    f"/products/{record['id']}/variations",
                    {"orderby": "id", "order": "asc"},
                    endpoint="/products/{id}/variations",
                )
            return render_product(record, variations)
        if kind.name == "reviews":
            return render_review(record, include_reviewer_names=self.options.include_reviewer_names)
        if kind.name == "orders":
            return render_order(record, self._order_notes(record["id"]))
        return render_customer(record)

    def _order_notes(self, order_id: Any) -> list[dict]:
        note_type = "any" if self.options.include_private_notes else "customer"
        _, body, _ = self.client.get(
            f"/orders/{order_id}/notes", {"type": note_type}, endpoint="/orders/{id}/notes"
        )
        if not isinstance(body, list):
            raise WooCommerceResponseError(
                f"WooCommerce /orders/{{id}}/notes: a {type(body).__name__}, not a list"
            )
        return [note for note in body if isinstance(note, dict) and not _is_system_note(note)]

    # -- 3. delete, 4. commit -----------------------------------------------
    def deletions(self) -> Iterator[dict[str, Any]]:
        for kind in self.kinds:
            plan = self.plans[kind.name]
            for record_id in plan["gone"]:
                plan["known"].pop(record_id, None)
                self.stats["deleted"] += 1
                yield _deleted_row(f"{kind.prefix}:{record_id}")
        # A kind that was synced before but is no longer selected left the
        # selection: its documents are forgotten like deleted ones.
        selected = {kind.name for kind in self.kinds}
        for name in sorted(set(self.state) - selected):
            if name not in _KINDS:
                continue
            for record_id in sorted((self.state.get(name) or {}).get("known") or {}, key=_id_order):
                self.stats["deleted"] += 1
                yield _deleted_row(f"{_KINDS[name].prefix}:{record_id}")

    def commit(self) -> None:
        for name in [n for n in self.state if n in _KINDS and n not in self.plans]:
            del self.state[name]
        for name, plan in self.plans.items():
            self.state[name] = {
                "cursor": plan["cursor"],
                "known": plan["known"],
                "retry": plan.get("kept", []),
            }


def _is_system_note(note: dict) -> bool:
    """A private note WooCommerce wrote itself ("Order status changed ...", email failures).

    WooCommerce signs its own notes as "WooCommerce" (``system`` on some
    versions); a note staff add in the admin carries their name. Notes sent to
    the customer are always kept.
    """
    return not note.get("customer_note") and note.get("author") in ("WooCommerce", "system")


def _id_order(record_id: str) -> tuple[int, str]:
    return (int(record_id), "") if record_id.isdigit() else (0, record_id)


def sync_store(
    client: _Client,
    state: dict,
    *,
    resources: Iterable[str] = DEFAULT_RESOURCES,
    product_status: str = "publish",
    include_variations: bool = False,
    include_private_notes: bool = False,
    include_reviewer_names: bool = False,
    order_notes_window_days: int = 7,
    stats: dict[str, int] | None = None,
    now: datetime | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield changed documents since the last run, then hard-delete markers.

    1. *List.* Every selected kind is listed, and every id that left a listing
       is confirmed gone with a GET. A failure here propagates before a single row.
    2. *Render.* Each new or changed record becomes a document; one whose text
       did not change is skipped. A record that fails to load is kept for the
       next run (three in a row stop the run as an outage).
    3. *Delete.* A hard-delete marker for every confirmed deletion.
    4. *Commit.* Only now is ``state`` written.
    """
    kinds = _selected_kinds(resources)
    stats = {} if stats is None else stats
    for key in _STAT_KEYS:
        stats.setdefault(key, 0)
    options = _Options(
        product_status=product_status,
        include_variations=include_variations,
        include_private_notes=include_private_notes,
        include_reviewer_names=include_reviewer_names,
        order_notes_window_days=order_notes_window_days,
    )
    run = _Run(client, state, kinds, options, stats, now)
    run.list_all()
    yield from run.render()
    yield from run.deletions()
    run.commit()
    logger.info(
        "WooCommerce: %d document(s) rendered, %d unchanged, %d deletion(s), %d kept for retry.",
        stats["rendered"],
        stats["unchanged"],
        stats["deleted"],
        stats["kept"],
    )


def _selected_kinds(resources: Iterable[str]) -> list[_Kind]:
    if isinstance(resources, str):
        resources = [resources]
    names = list(dict.fromkeys(resources))
    if not names:
        raise ValueError("Select at least one of: " + ", ".join(RESOURCES))
    unknown = [name for name in names if name not in _KINDS]
    if unknown:
        raise ValueError(
            f"Unknown WooCommerce resource(s) {unknown}; choose from {', '.join(RESOURCES)}"
        )
    return [_KINDS[name] for name in names]


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def woocommerce_source(
    store_url: str | None = None,
    consumer_key: str | None = None,
    consumer_secret: str | None = None,
    resources: Iterable[str] = DEFAULT_RESOURCES,
    *,
    product_status: str = "publish",
    include_variations: bool = False,
    include_private_notes: bool = False,
    include_reviewer_names: bool = False,
    order_notes_window_days: int = 7,
    service: Any = None,
    session: Any = None,
    resource_name: str = WOOCOMMERCE_TABLE_NAME,
    check_active: Callable[[], None] | None = None,
):
    """Return a ``dlt`` resource that yields WooCommerce documents for ``remember``.

    Args:
        store_url: The store's address. Falls back to ``WOOCOMMERCE_STORE_URL``.
            ``https://`` uses basic auth; ``http://`` (a local store) signs every
            request with OAuth 1.0a, the only way WooCommerce takes keys over HTTP.
        consumer_key, consumer_secret: A REST API key with **Read** permission.
            Fall back to ``WOOCOMMERCE_CONSUMER_KEY`` / ``WOOCOMMERCE_CONSUMER_SECRET``.
        resources: Any of ``products``, ``reviews``, ``orders``, ``customers``.
            Customers are personal data and are not synced unless listed here.
        product_status: Which products are in scope (``publish`` by default;
            ``any`` for every status but trash). A product that leaves the scope
            is forgotten.
        include_variations: Add each variable product's variations to its document.
        include_private_notes: Add private order notes, not just the notes sent
            to the customer.
        include_reviewer_names: Add the reviewer's display name to reviews.
        order_notes_window_days: Orders modified within this many days are read
            again on every run, because a new order note does not change an
            order's modification date. ``0`` turns this off (notes are then only
            picked up when the order itself changes).
        service: Pre-built ``requests``-like session, mainly for tests; when
            omitted one is built for each run and closed after it. ``session`` is
            accepted as an alias.
        resource_name: dlt resource (table) name, which also scopes the sync
            state. Give each store its own name when several share a dataset.
        check_active: Optional host authorization checkpoint, called before every
            request and wait and around every row; it stops the sync by raising.

    Returns:
        A ``dlt`` resource with ``primary_key="id"``, ``write_disposition="merge"``
        and a ``_deleted`` hard-delete column. ``resource.cognee_sync_stats``
        holds the last run's counts.
    """
    if not all(
        hasattr(dlt_utils, attr)
        for attr in ("PIPELINE_SCOPE_ATTR", "guarded_rows", "NODE_SET_COLUMN")
    ):
        raise RuntimeError(
            "The WooCommerce connector needs cognee's dlt_utils.PIPELINE_SCOPE_ATTR, "
            "guarded_rows and NODE_SET_COLUMN (cognee>=1.6.3)."
        )
    _check_resource_name(resource_name)
    if service is not None and session is not None:
        raise ValueError("Pass service= or session=, not both")
    service = service if service is not None else session
    kinds = _selected_kinds(resources)  # fail on a bad selection now, not mid-run
    if not isinstance(product_status, str) or not product_status:
        raise ValueError("product_status must be a product status such as 'publish' or 'any'")
    if (
        not isinstance(order_notes_window_days, int)
        or isinstance(order_notes_window_days, bool)
        or not 0 <= order_notes_window_days <= 365
    ):
        raise ValueError("order_notes_window_days must be a whole number of days from 0 to 365")

    url = _normalize_store_url(store_url or os.environ.get("WOOCOMMERCE_STORE_URL") or "")
    key = consumer_key or os.environ.get("WOOCOMMERCE_CONSUMER_KEY") or ""
    secret = consumer_secret or os.environ.get("WOOCOMMERCE_CONSUMER_SECRET") or ""
    if not key or not secret:
        raise ValueError(
            "WooCommerce REST API key required: pass consumer_key= and consumer_secret= "
            "or set WOOCOMMERCE_CONSUMER_KEY and WOOCOMMERCE_CONSUMER_SECRET."
        )
    _check_credential(key, "consumer key")
    _check_credential(secret, "consumer secret")
    host = urlsplit(url).hostname or ""
    if url.startswith("http://") and host not in ("localhost", "127.0.0.1", "::1"):
        logger.warning(
            "WooCommerce: %s is plain HTTP; requests are signed, but store data travels "
            "unencrypted. Use https:// outside local development.",
            host,
        )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        # Rows with _deleted=True are removed from the destination on merge,
        # which propagates the deletion through cognee's orphan_cleanup.
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def woocommerce_documents():
        stats.clear()
        http = service if service is not None else requests.Session()
        try:
            client = _Client(http, url, key, secret, check_active)
            yield from dlt_utils.guarded_rows(_rows(client), check_active)
        finally:
            if service is None:
                http.close()

    def _rows(client: _Client) -> Iterator[dict[str, Any]]:
        state = dlt.current.resource_state()
        # One resource_name holds one store's state; a second store under the
        # same name would see the first store's records as deleted.
        if state.get("store_url") not in (None, url):
            raise ValueError(
                f"resource_name {resource_name!r} already syncs another store; "
                "give each store its own resource_name"
            )
        state["store_url"] = url
        yield from sync_store(
            client,
            state,
            resources=[kind.name for kind in kinds],
            product_status=product_status,
            include_variations=include_variations,
            include_private_notes=include_private_notes,
            include_reviewer_names=include_reviewer_names,
            order_notes_window_days=order_notes_window_days,
            stats=stats,
        )

    resource = woocommerce_documents()
    # Opt into the document ingestion path (row -> text document -> cognify).
    setattr(resource, DOCUMENT_SOURCE_ATTR, WOOCOMMERCE_SOURCE_NAME)
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
