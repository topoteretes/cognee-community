"""Unit tests for the Shopify connector.

The GraphQL Admin API is mocked by ``FakeShopify``, an in-memory stand-in for a
``requests`` session, so no store and no network are needed. It answers the
client credentials grant, checks the ``X-Shopify-Access-Token`` of every
GraphQL call, pages connections with ``first``/``after`` cursors, reports a
throttle bucket in ``extensions.cost``, keeps a destroy-event feed, answers
``null`` from ``nodes(ids:)`` for deleted ids, and sends a ``Date`` header.

Three layers:

* pure tests for rendering, the shop name and the GraphQL helpers;
* ``sync_store`` driven with a plain dict as state (ingest, change detection,
  renames, deletion, orders, token refresh, and the failure paths that must not
  delete anything);
* a real ``dlt`` pipeline into a temp sqlite destination (edit and delete
  across runs, two sources in one dataset, ``check_active``, a kept item).
"""

import copy
import json
import logging
import random
import re
from email.utils import format_datetime
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest
import requests
from cognee.tasks.ingestion.dlt_utils import document_source_tag, pipeline_name_for_source
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from dlt.pipeline.exceptions import PipelineStepFailed

from cognee_community_connector_shopify import shopify
from cognee_community_connector_shopify.shopify import (
    ShopifyAPIError,
    ShopifyResponseError,
    _Client,
    _render_collection,
    _render_product,
    normalize_shop,
    shopify_source,
    sync_store,
)

SHOP = "acme.myshopify.com"
NOW = "2026-10-17T12:00:00Z"
CLIENT_SECRET = "shpss_test_secret_value"


def at(day, second=0):
    """A deterministic Shopify-style timestamp, ``at(1) < at(1, 5) < at(2)``."""
    return f"2026-10-{day:02d}T10:00:{second:02d}Z"


DAY1 = at(1)


def gid(kind, n):
    return f"gid://shopify/{kind}/{n}"


# ---------------------------------------------------------------------------
# Fake GraphQL Admin API
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload
        self.text = "" if payload is None or payload is _NOT_JSON else json.dumps(payload)

    def json(self):
        if self._payload is _NOT_JSON:
            raise ValueError("Expecting value")
        return self._payload


_NOT_JSON = object()


class FakeShopify:
    """Minimal stand-in for a ``requests`` session talking to one shop."""

    def __init__(self):
        self.products = {}  # gid -> product (as product(id:) returns it)
        self.collections = {}  # gid -> collection fields + "members": [product gids]
        self.customers = {}
        self.orders = {}
        self.events = []  # BasicEvent dicts, ids ascending
        self.deleted = set()  # gids nodes(ids:) answers null for
        self.unlisted = set()  # gids left out of the sweep that still resolve
        self.page_size = 250
        self.version = shopify._API_VERSION  # what X-Shopify-API-Version says
        self.clock = NOW  # what the Date header says (None: no header)
        self.tokens = set()  # tokens the API accepts right now
        self.grants = 0
        self.calls = []  # (operation, variables) of every GraphQL call
        self._queued = []  # (operation, predicate, response) answered once

    # -- fixture builders ----------------------------------------------------
    def add_product(self, n, *, updated_at=DAY1, **fields):
        self.products[gid("Product", n)] = {
            "id": gid("Product", n),
            "title": f"Product {n}",
            "status": "ACTIVE",
            "vendor": "Acme",
            "productType": "Shirt",
            "tags": [],
            "handle": f"product-{n}",
            "onlineStoreUrl": None,
            "description": "",
            "options": [{"name": "Title", "values": ["Default Title"]}],
            "variants": [
                {
                    "id": gid("ProductVariant", n * 10),
                    "title": "Default Title",
                    "sku": f"SKU-{n}",
                    "price": "10.00",
                    "position": 1,
                }
            ],
            **fields,
            "updatedAt": updated_at,
        }

    def add_collection(self, n, members=(), *, rules=None, updated_at=DAY1, **fields):
        self.collections[gid("Collection", n)] = {
            "id": gid("Collection", n),
            "updatedAt": updated_at,
            "title": f"Collection {n}",
            "handle": f"collection-{n}",
            "description": "",
            "ruleSet": rules,
            "members": [gid("Product", m) for m in members],
            **fields,
        }

    def add_customer(self, n, *, updated_at=DAY1, **fields):
        self.customers[gid("Customer", n)] = {
            "id": gid("Customer", n),
            "displayName": f"Customer {n}",
            "tags": [],
            "note": "",
            "state": "ENABLED",
            "defaultEmailAddress": {"emailAddress": f"c{n}@example.com"},
            "defaultAddress": {"city": "Berlin", "country": "Germany"},
            **fields,
            "updatedAt": updated_at,
        }

    def add_order(self, n, *, customer=None, products=(), updated_at=DAY1, created_at=None):
        self.orders[gid("Order", n)] = {
            "id": gid("Order", n),
            "name": f"#{1000 + n}",
            "createdAt": created_at or updated_at,
            "updatedAt": updated_at,
            "displayFinancialStatus": "PAID",
            "displayFulfillmentStatus": "UNFULFILLED",
            "customer": {"id": gid("Customer", customer)} if customer else None,
            "totalPriceSet": {"shopMoney": {"amount": "20.0", "currencyCode": "EUR"}},
            "lineItems": {
                "nodes": [
                    {"title": f"Product {p}", "quantity": 1, "product": {"id": gid("Product", p)}}
                    for p in products
                ]
            },
        }

    def destroy(self, kind, n):
        """Delete an object the way Shopify does: gone, null in nodes(), a destroy event."""
        key = gid(kind, n)
        {
            "Product": self.products,
            "Collection": self.collections,
            "Customer": self.customers,
            "Order": self.orders,
        }[kind].pop(key, None)
        self.deleted.add(key)
        self.events.append(
            {
                "id": gid("BasicEvent", 100 + len(self.events)),
                "action": "destroy",
                "subjectId": key,
                "subjectType": kind.upper(),
            }
        )

    def queue(self, operation, response, when=lambda variables: True):
        """Answer the next matching call with ``response`` instead of real data."""
        self._queued.append((operation, when, response))

    def ops(self, operation):
        return [variables for op, variables in self.calls if op == operation]

    def fetched(self, operation):
        return sorted(int(v["id"].rsplit("/", 1)[1]) for v in self.ops(operation))

    def expire(self):
        """Revoke every issued token, as an expired 24 h token would be."""
        self.tokens.clear()

    # -- requests.Session surface ----------------------------------------------
    def post(self, url, json=None, data=None, headers=None, timeout=None):
        assert url.startswith(f"https://{SHOP}/admin/"), f"unexpected URL: {url}"
        assert timeout, "every request must carry a timeout"
        headers = headers or {}
        if url.endswith("/admin/oauth/access_token"):
            assert json is None, "the grant is form-encoded"
            assert data["grant_type"] == "client_credentials"
            if (data["client_id"], data["client_secret"]) != ("app-id", CLIENT_SECRET):
                return _Resp(400, {"error": "invalid_client"})
            self.grants += 1
            token = f"shpat_issued_{self.grants}"
            self.tokens.add(token)
            return _Resp(
                200, {"access_token": token, "scope": "read_products", "expires_in": 86399}
            )
        assert url == f"https://{SHOP}/admin/api/{shopify._API_VERSION}/graphql.json"
        assert "client_secret" not in str(json), "credentials never go to the GraphQL API"
        operation = re.search(r"query (\w+)", json["query"]).group(1)
        variables = json.get("variables") or {}
        self.calls.append((operation, variables))
        if headers.get("X-Shopify-Access-Token") not in self.tokens:
            return _Resp(401, {"errors": "[API] Invalid API key or access token"})
        for index, (op, when, response) in enumerate(self._queued):
            if op == operation and when(variables):
                del self._queued[index]
                return response
        cost = estimated_cost(json["query"])
        if cost > 1000:
            message = f"Query cost is {cost}, exceeding max 1000"
            return _Resp(
                200, {"errors": [{"message": message, "extensions": {"code": "MAX_COST_EXCEEDED"}}]}
            )
        data_ = self._route(operation, variables)
        body = {
            "data": data_,
            "extensions": {
                "cost": {
                    "requestedQueryCost": cost,
                    "actualQueryCost": 5,
                    "throttleStatus": {
                        "maximumAvailable": 2000.0,
                        "currentlyAvailable": 1990.0,
                        "restoreRate": 100.0,
                    },
                }
            },
        }
        headers = {"X-Shopify-API-Version": self.version}
        if self.clock:  # None: an answer without a Date header
            headers["Date"] = _http_date(self.clock)
        return _Resp(200, body, headers)

    def _route(self, operation, v):
        if operation == "ProductSweep":
            nodes = [
                {k: p[k] for k in ("id", "updatedAt", "title")}
                for p in self.products.values()
                if p["id"] not in self.unlisted
            ]
            return {"products": self._page(nodes, v)}
        if operation == "Product":
            product = self.products.get(v["id"])
            if product is None:
                return {"product": None}
            out = {k: val for k, val in product.items() if k != "variants"}
            out["variants"] = {"nodes": product["variants"][:100]}
            out["variantsCount"] = {"count": len(product["variants"])}
            return {"product": out}
        if operation == "CollectionSweep":
            nodes = [
                {"id": c["id"], "updatedAt": c["updatedAt"]} for c in self.collections.values()
            ]
            return {"collections": self._page(nodes, v)}
        if operation == "Collection":
            c = self.collections.get(v["id"])
            if c is None:
                return {"collection": None}
            out = {k: val for k, val in c.items() if k != "members"}
            out["products"] = {"nodes": [{"id": m} for m in c["members"][:250]]}
            out["productsCount"] = {"count": len(c["members"])}
            return {"collection": out}
        if operation == "CustomerSweep":
            nodes = [
                {k: c[k] for k in ("id", "updatedAt", "displayName")}
                for c in self.customers.values()
            ]
            return {"customers": self._page(nodes, v)}
        if operation == "Customer":
            return {"customer": copy.deepcopy(self.customers.get(v["id"]))}
        if operation == "Orders":
            orders = sorted(self.orders.values(), key=lambda o: (o["updatedAt"], o["id"]))
            if v.get("query"):
                since = re.match(r"updated_at:>='(.+)'$", v["query"]).group(1)
                orders = [o for o in orders if o["updatedAt"] >= since]  # inclusive
            return {"orders": self._page(orders, v, sort=False)}
        if operation == "Order":
            return {"order": copy.deepcopy(self.orders.get(v["id"]))}
        if operation == "LatestEvent":
            assert v["query"] == "action:destroy"
            nodes = [{"id": e["id"]} for e in self.events[-1:]]
            return {
                "events": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}
            }
        if operation == "DestroyEvents":
            match = re.match(r"action:destroy AND id:>(\d+)$", v["query"])
            assert match, v["query"]
            after_id = int(match.group(1))
            events = [e for e in self.events if int(e["id"].rsplit("/", 1)[1]) > after_id]
            return {"events": self._page(events, v, sort=False)}
        if operation == "Nodes":
            live = {**self.products, **self.collections, **self.customers, **self.orders}
            return {
                "nodes": [
                    None if i in self.deleted or i not in live else {"id": i} for i in v["ids"]
                ]
            }
        raise AssertionError(f"unexpected operation {operation}")

    def _page(self, nodes, v, sort=True):
        if sort:
            nodes = sorted(nodes, key=lambda n: int(n["id"].rsplit("/", 1)[1]))
        start = int((v.get("after") or "c0")[1:])
        end = start + self.page_size
        more = end < len(nodes)
        return {
            "nodes": copy.deepcopy(nodes[start:end]),
            "pageInfo": {"hasNextPage": more, "endCursor": f"c{end}" if more else None},
        }


def estimated_cost(query):
    """Shopify's rule of thumb: a connection costs its ``first``, nested ones multiply.

    Close enough to Shopify's own calculation to catch a query over the limit.
    """
    firsts = [int(n) for n in re.findall(r"first: (\d+)", query)]
    if not firsts:
        return 1
    outer, *inner = firsts
    return outer * (1 + sum(inner)) if inner else outer + 2


def _http_date(stamp):
    return format_datetime(shopify.datetime.fromisoformat(stamp), usegmt=True)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record retry waits instead of sleeping."""
    recorded = []
    monkeypatch.setattr(shopify.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def fake():
    return FakeShopify()


@pytest.fixture
def store(fake):
    """Three products, a custom collection of two of them, a smart collection."""
    fake.add_product(1, title="Linen shirt", tags=["summer", "linen"])
    fake.add_product(2, title="Wool sweater")
    fake.add_product(3, title="Canvas tote")
    fake.add_collection(10, [2, 1], title="Bestsellers")
    fake.add_collection(
        11,
        [3],
        title="Bags",
        rules={
            "appliedDisjunctively": False,
            "rules": [{"column": "TYPE", "relation": "EQUALS", "condition": "Bag"}],
        },
    )
    return fake


def client_for(fake, token="static"):
    if token == "static":
        fake.tokens.add("shpat_static")
        return _Client(fake, SHOP, "shpat_static", None, None)
    return _Client(fake, SHOP, None, "app-id", CLIENT_SECRET)


def run_sync(fake, state, client=None, now=NOW, **kwargs):
    return list(run_sync_iter(fake, state, client, now, **kwargs))


def run_sync_iter(fake, state, client=None, now=NOW, **kwargs):
    fake.calls.clear()
    client = client or client_for(fake)
    return sync_store(client, state, now=shopify.datetime.fromisoformat(now), **kwargs)


def docs(rows, prefix=""):
    return {r["id"]: r for r in rows if not r["_deleted"] and r["id"].startswith(prefix)}


def tombstones(rows):
    return sorted(r["id"] for r in rows if r["_deleted"])


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------
PRODUCT = {
    "id": gid("Product", 7),
    "title": "Linen shirt",
    "status": "ACTIVE",
    "vendor": "Acme",
    "productType": "Shirt",
    "tags": ["summer", "linen"],
    "handle": "linen-shirt",
    "onlineStoreUrl": "https://acme.example/products/linen-shirt",
    "description": "Breathable linen. ",
    "options": [{"name": "Size", "values": ["S", "M"]}, {"name": "Color", "values": ["Sand"]}],
    "variants": {
        "nodes": [
            {
                "id": gid("ProductVariant", 2),
                "title": "M / Sand",
                "sku": "LS-M",
                "price": "49.00",
                "position": 2,
            },
            {
                "id": gid("ProductVariant", 1),
                "title": "S / Sand",
                "sku": "LS-S",
                "price": "49.00",
                "position": 1,
            },
        ]
    },
    "variantsCount": {"count": 2},
    "totalInventory": 37,  # never rendered
}


def test_render_product_has_header_description_and_variants_but_no_inventory():
    assert _render_product(PRODUCT) == (
        "Type: product\n"
        "Status: active\n"
        "Vendor: Acme\n"
        "Product type: Shirt\n"
        "Tags: linen, summer\n"
        "Handle: linen-shirt\n"
        "URL: https://acme.example/products/linen-shirt\n"
        "Option Color: Sand\n"
        "Option Size: S, M\n"
        "\n"
        "Breathable linen.\n"
        "\n"
        "Variants:\n"
        "- S / Sand, SKU LS-S, price 49.00\n"
        "- M / Sand, SKU LS-M, price 49.00"
    )


def test_render_product_without_variants_skips_the_default_option():
    product = {
        "title": "Gift card",
        "status": "DRAFT",
        "options": [{"name": "Title", "values": ["Default Title"]}],
        "variants": {"nodes": [{"title": "Default Title", "price": "25.00", "position": 1}]},
    }
    assert _render_product(product) == (
        "Type: product\nStatus: draft\n\nVariants:\n- Default Title, price 25.00"
    )


def test_render_product_caps_variants_and_says_how_many_more():
    variants = [
        {"id": gid("ProductVariant", i), "title": f"V{i}", "position": i} for i in range(1, 101)
    ]
    text = _render_product({"variants": {"nodes": variants}, "variantsCount": {"count": 340}})
    assert text.count("\n- V") == 100
    assert text.endswith("- V100\n- ... and 240 more variants")


def test_render_collection_lists_members_by_current_title_and_its_rules():
    titles = {gid("Product", 1): "Linen shirt", gid("Product", 2): "Wool sweater"}
    smart = {
        "title": "Summer",
        "handle": "summer",
        "description": "Hot picks.",
        "ruleSet": {
            "appliedDisjunctively": True,
            "rules": [
                {"column": "TAG", "relation": "EQUALS", "condition": "summer"},
                {"column": "TYPE", "relation": "EQUALS", "condition": "Shirt"},
            ],
        },
        "products": {"nodes": [{"id": gid("Product", 2)}, {"id": gid("Product", 1)}]},
        "productsCount": {"count": 2},
    }
    assert _render_collection(smart, titles) == (
        "Type: collection\n"
        "Kind: smart (tag equals summer or type equals shirt)\n"
        "Handle: summer\n"
        "\n"
        "Hot picks.\n"
        "\n"
        "Products:\n"
        "- Linen shirt\n"
        "- Wool sweater"
    )
    custom = {**smart, "ruleSet": None, "productsCount": {"count": 300}}
    assert "Kind: custom" in _render_collection(custom, titles)
    assert _render_collection(custom, titles).endswith("- ... and 298 more products")


def test_shuffled_lists_render_the_same_text():
    rng = random.Random(7)
    titles = {gid("Product", i): f"P{i}" for i in range(6)}
    collection = {"products": {"nodes": [{"id": gid("Product", i)} for i in range(6)]}}
    for _ in range(5):
        shuffled_product = {
            **PRODUCT,
            "tags": rng.sample(PRODUCT["tags"], 2),
            "options": rng.sample(PRODUCT["options"], 2),
            "variants": {"nodes": rng.sample(PRODUCT["variants"]["nodes"], 2)},
        }
        assert _render_product(shuffled_product) == _render_product(PRODUCT)
        shuffled = {"products": {"nodes": rng.sample(collection["products"]["nodes"], 6)}}
        assert _render_collection(shuffled, titles) == _render_collection(collection, titles)


@pytest.mark.parametrize(
    "shop", ["acme", "ACME", "acme.myshopify.com", "https://acme.myshopify.com/"]
)
def test_shop_is_normalized(shop):
    assert normalize_shop(shop) == SHOP


@pytest.mark.parametrize("shop", ["", "shop.acme.com", "acme.myshopify.com.evil.io", "a/b", "-x"])
def test_shop_outside_myshopify_is_refused(shop):
    with pytest.raises(ValueError, match="myshopify"):
        normalize_shop(shop)


def test_admin_url_stays_in_the_shop_admin():
    assert (
        shopify._admin_url(SHOP, "api/x/graphql.json") == f"https://{SHOP}/admin/api/x/graphql.json"
    )


# ---------------------------------------------------------------------------
# GraphQL client
# ---------------------------------------------------------------------------
def test_graphql_errors_with_http_200_raise(fake):
    fake.queue("ProductSweep", _Resp(200, {"errors": [{"message": "Field 'x' doesn't exist"}]}))
    with pytest.raises(ShopifyAPIError) as excinfo:
        shopify._list_all(client_for(fake), shopify._PRODUCT_SWEEP, "products")
    assert excinfo.value.status == 200
    assert str(excinfo.value) == "Shopify ProductSweep: GraphQL error"
    assert excinfo.value.detail == "Field 'x' doesn't exist"


def test_throttled_waits_from_the_reported_bucket_then_succeeds(fake, sleeps):
    throttled = _Resp(
        200,
        {
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {
                "cost": {
                    "throttleStatus": {
                        "maximumAvailable": 1000.0,
                        "currentlyAvailable": 10.0,
                        "restoreRate": 50.0,
                    }
                }
            },
        },
    )
    fake.queue("ProductSweep", throttled)
    assert shopify._list_all(client_for(fake), shopify._PRODUCT_SWEEP, "products") == []
    assert sleeps == [(260 - 10) / 50]


def test_low_budget_waits_before_the_next_query(fake, sleeps):
    client = client_for(fake)
    client.note_cost(
        {"cost": {"throttleStatus": {"currentlyAvailable": 60.0, "restoreRate": 50.0}}}
    )
    shopify._list_all(client, shopify._PRODUCT_SWEEP, "products")
    assert sleeps == [(260 - 60) / 50]


def test_http_429_waits_retry_after(fake, sleeps):
    fake.queue("ProductSweep", _Resp(429, {"errors": "Exceeded"}, {"Retry-After": "2.0"}))
    shopify._list_all(client_for(fake), shopify._PRODUCT_SWEEP, "products")
    assert sleeps == [2.0]


def test_exhausted_throttle_raises_rate_limited(fake):
    for _ in range(shopify._MAX_RETRIES):
        fake.queue("ProductSweep", _Resp(429, {"errors": "Exceeded"}, {"Retry-After": "1"}))
    with pytest.raises(ShopifyAPIError) as excinfo:
        shopify._list_all(client_for(fake), shopify._PRODUCT_SWEEP, "products")
    assert excinfo.value.rate_limited


@pytest.mark.parametrize(
    "response",
    [
        _Resp(200, _NOT_JSON),
        _Resp(200, ["not", "an", "object"]),
        _Resp(200, {"extensions": {}}),  # no data
        _Resp(200, {"data": {"products": {"nodes": []}}}),  # no pageInfo
        _Resp(200, {"data": {"products": {"nodes": [], "pageInfo": {"hasNextPage": True}}}}),
    ],
)
def test_body_of_the_wrong_shape_raises_with_state_unchanged(store, response):
    state = {}
    run_sync(store, state)
    before = copy.deepcopy(state)
    store.destroy("Product", 1)  # a real deletion is pending; it must wait
    store.queue("ProductSweep", response)

    with pytest.raises(ShopifyResponseError):
        run_sync(store, state)
    assert state == before


# ---------------------------------------------------------------------------
# sync_store: ingest and change detection
# ---------------------------------------------------------------------------
def test_first_sync_renders_products_and_collections_only(store):
    store.add_customer(5)
    store.add_order(6, customer=5, products=[1])
    state = {}
    rows = run_sync(store, state)

    assert set(docs(rows)) == {
        "product:1",
        "product:2",
        "product:3",
        "collection:10",
        "collection:11",
    }
    assert docs(rows)["collection:10"]["content"].endswith(
        "Products:\n- Linen shirt\n- Wool sweater"
    )
    assert not store.ops("CustomerSweep") and not store.ops("Orders")
    assert state["event_cursor"] == "0"  # no events yet


def test_quiet_run_renders_nothing(store):
    state = {}
    run_sync(store, state)
    rows = run_sync(store, state)

    assert rows == []
    assert store.ops("Product") == []  # no product fetched in full
    assert len(store.ops("Collection")) == 2  # collections are read every run


def test_edited_product_re_renders(store):
    state = {}
    run_sync(store, state)
    store.products[gid("Product", 2)].update(description="Now in navy.", updated_at=at(5))
    store.products[gid("Product", 2)]["updatedAt"] = at(5)

    rows = run_sync(store, state)

    assert set(docs(rows)) == {"product:2"}
    assert "Now in navy." in docs(rows)["product:2"]["content"]


def test_renamed_product_re_renders_the_collections_that_list_it(store):
    state = {}
    run_sync(store, state)
    store.products[gid("Product", 1)].update(title="Linen shirt (new)", updatedAt=at(5))

    rows = run_sync(store, state)

    assert set(docs(rows)) == {"product:1", "collection:10"}
    assert "- Linen shirt (new)" in docs(rows)["collection:10"]["content"]


def test_membership_change_re_renders_the_collection(store):
    state = {}
    run_sync(store, state)
    store.collections[gid("Collection", 10)]["members"].append(gid("Product", 3))

    rows = run_sync(store, state)

    assert set(docs(rows)) == {"collection:10"}
    assert "- Canvas tote" in docs(rows)["collection:10"]["content"]


def test_copy_fetched_soon_after_its_stamp_is_fetched_once_more(store):
    store.clock = at(1, 30)  # 30 s after the products' updatedAt
    state = {}
    run_sync(store, state)
    store.clock = NOW

    run_sync(store, state)
    assert store.fetched("Product") == [1, 2, 3]
    run_sync(store, state)
    assert store.fetched("Product") == []


# ---------------------------------------------------------------------------
# sync_store: deletion
# ---------------------------------------------------------------------------
def test_deleted_product_with_a_destroy_event_is_forgotten(store):
    state = {}
    run_sync(store, state)
    store.destroy("Product", 2)

    rows = run_sync(store, state)

    assert tombstones(rows) == ["product:2"]
    assert store.ops("Nodes") == []  # the event was enough
    assert "- Wool sweater" not in docs(rows)["collection:10"]["content"]


def test_missing_product_with_a_null_node_is_forgotten(store):
    state = {}
    run_sync(store, state)
    store.destroy("Product", 2)
    store.events.clear()  # no event (retention, delivery): nodes() decides

    assert tombstones(run_sync(store, state)) == ["product:2"]


def test_missing_product_that_still_resolves_is_kept_and_asked_again(store):
    state = {}
    run_sync(store, state)
    # Missing from the sweep, yet nodes() still finds it: not proven gone.
    store.unlisted.add(gid("Product", 2))

    rows = run_sync(store, state)
    assert tombstones(rows) == []
    assert "product:2" in state["known_ids"]
    assert len(store.ops("Nodes")) == 1
    run_sync(store, state)
    assert len(store.ops("Nodes")) == 1  # asked again on the next run


def test_missing_ids_whose_check_fails_are_kept(store):
    state = {}
    run_sync(store, state)
    store.destroy("Product", 2)
    store.events.clear()
    store.queue("Nodes", _Resp(500, {"errors": "boom"}))
    for _ in range(shopify._MAX_RETRIES - 1):
        store.queue("Nodes", _Resp(500, {"errors": "boom"}))

    rows = run_sync(store, state)
    assert tombstones(rows) == []
    assert "product:2" in state["known_ids"]
    assert tombstones(run_sync(store, state)) == ["product:2"]  # asked again, now confirmed


def test_deleted_collection_is_forgotten(store):
    state = {}
    run_sync(store, state)
    store.destroy("Collection", 11)

    assert tombstones(run_sync(store, state)) == ["collection:11"]


@pytest.mark.parametrize("failing", ["ProductSweep", "CollectionSweep", "DestroyEvents"])
def test_failed_listing_deletes_nothing_and_keeps_state(store, failing):
    state = {}
    run_sync(store, state)
    store.destroy("Product", 3)  # creates an event, so DestroyEvents is asked
    run_sync(store, state)
    before = copy.deepcopy(state)
    store.destroy("Product", 1)
    status = 500 if failing == "DestroyEvents" else 403  # an events 403 falls back (below)
    for _ in range(shopify._MAX_RETRIES if status == 500 else 1):
        store.queue(failing, _Resp(status, {"errors": [{"message": "Access denied"}]}))

    rows = []
    with pytest.raises(ShopifyAPIError):
        for row in sync_store(client_for(store), state):
            rows.append(row)
    assert rows == []
    assert state == before
    assert tombstones(run_sync(store, state)) == ["product:1"]


@pytest.mark.parametrize(
    "refusal",
    [
        _Resp(403, {"errors": [{"message": "Access denied for events field."}]}),
        _Resp(
            200,
            {
                "errors": [
                    {
                        "message": "Access denied for events field.",
                        "extensions": {"code": "ACCESS_DENIED"},
                    }
                ]
            },
        ),
    ],
)
def test_events_the_token_may_not_read_fall_back_to_nodes(shop_with_orders, refusal, caplog):
    state = {}
    run_sync(shop_with_orders, state, resources=ALL)
    shop_with_orders.destroy("Product", 2)
    shop_with_orders.destroy("Order", 7)
    shop_with_orders.queue("DestroyEvents", refusal)

    rows = run_sync(shop_with_orders, state, resources=ALL)

    # Both are still confirmed gone through nodes().
    assert tombstones(rows) == ["order:7", "product:2"]
    assert state["event_cursor"] == "0"  # not moved: those events are read next time
    assert "may not read events" in caplog.text


def test_every_query_stays_under_the_cost_limit():
    queries = [v for v in vars(shopify).values() if isinstance(v, str) and "query " in v[:20]]
    assert len(queries) == 11
    assert set(queries) == set(shopify._COSTS)  # each has a cost to wait for
    for query in queries:
        assert estimated_cost(query) <= 1000, shopify._operation(query)


def test_a_query_over_the_cost_limit_is_named_and_not_kept(store, monkeypatch):
    monkeypatch.setattr(shopify, "_PRODUCT", shopify._PRODUCT.replace("first: 100", "first: 25000"))
    state = {}
    with pytest.raises(shopify.ShopifyQueryCostError, match="Product query"):
        run_sync(store, state)
    assert state == {}


def test_another_api_version_is_warned_about_once(store, caplog):
    store.version = "2026-07"
    client = client_for(store)
    run_sync(store, {}, client=client)
    assert caplog.text.count("served 2026-07") == 1
    assert client.served_version == "2026-07"
    assert client.requested_costs["ProductSweep"] == 252


def test_order_with_more_line_items_than_the_cap_says_so(shop_with_orders):
    shop_with_orders.add_order(9, products=range(1, 61), updated_at=at(3))
    for item in shop_with_orders.orders[gid("Order", 9)]["lineItems"]["nodes"]:
        item["product"] = None
    shop_with_orders.orders[gid("Order", 9)]["lineItems"]["pageInfo"] = {"hasNextPage": True}
    shop_with_orders.orders[gid("Order", 9)]["lineItems"]["nodes"] = shop_with_orders.orders[
        gid("Order", 9)
    ]["lineItems"]["nodes"][:50]

    rows = run_sync(shop_with_orders, {}, resources=ALL)
    assert docs(rows)["order:9"]["content"].endswith("- ... and more line items (over 50)")


# ---------------------------------------------------------------------------
# sync_store: per-item failures
# ---------------------------------------------------------------------------
def _fail(fake, operation, n, status=500):
    for _ in range(shopify._MAX_RETRIES):
        fake.queue(
            operation,
            _Resp(status, {"errors": "boom"}),
            when=lambda v, n=n: v.get("id", "").endswith(f"/{n}"),
        )


def test_product_that_fails_on_its_own_is_kept_and_retried(store):
    state = {}
    run_sync(store, state)
    for n in (1, 2):
        store.products[gid("Product", n)]["updatedAt"] = at(5)
    _fail(store, "Product", 1)

    rows = run_sync(store, state)
    assert set(docs(rows, "product:")) == {"product:2"}
    assert tombstones(rows) == []
    assert state["stamps"]["product:1"] == ""

    rows = run_sync(store, state)
    assert store.fetched("Product") == [1]
    assert set(docs(rows)) == {"product:1"}


def test_collection_that_fails_is_kept_and_emitted_next_run(store):
    state = {}
    run_sync(store, state)
    store.collections[gid("Collection", 10)]["members"].append(gid("Product", 3))
    _fail(store, "Collection", 10)

    assert docs(run_sync(store, state)) == {}
    assert "collection:10" in state["known_ids"] and state["texts"]["collection:10"] == ""
    assert set(docs(run_sync(store, state))) == {"collection:10"}


def _five_products(store):
    for n in (4, 5):
        store.add_product(n)
    state = {}
    run_sync(store, state)
    for n in (1, 2, 3, 4, 5):
        store.products[gid("Product", n)].update(description="v2", updatedAt=at(5))
    return state


def _on(n):
    return lambda v: v.get("id", "").endswith(f"/{n}")


@pytest.mark.parametrize("failure", ["401", "throttle", "long retry-after", "outage"])
def test_systemic_failure_stops_cleanly_and_resumes(store, failure, sleeps):
    state = _five_products(store)
    store.destroy("Product", 1)  # a real deletion waits for a run that finishes
    store.products.pop(gid("Product", 1), None)
    client = client_for(store, token="grant")
    if failure == "401":  # the refresh is tried once, then a second 401
        store.queue("Product", _Resp(401, {"errors": "Invalid"}), when=_on(4))
        store.queue("Product", _Resp(401, {"errors": "Invalid"}), when=_on(4))
    elif failure == "throttle":
        for _ in range(shopify._MAX_RETRIES):
            store.queue("Product", _Resp(429, {}, {"Retry-After": "1"}), when=_on(4))
    elif failure == "long retry-after":
        store.queue("Product", _Resp(429, {}, {"Retry-After": "3600"}), when=_on(4))
    else:
        for n in (3, 4, 5):
            _fail(store, "Product", n)
    stats = {}
    rows = run_sync(store, state, client=client, stats=stats)

    reasons = {"401": "auth", "throttle": "rate_limit", "long retry-after": "rate_limit"}
    assert stats[f"failed_{reasons.get(failure, 'unavailable')}"] == 1
    assert all(wait <= shopify._MAX_WAIT_SECONDS for wait in sleeps)
    assert tombstones(rows) == []  # nothing deleted on a stopped run
    assert "product:1" in state["known_ids"]
    if failure == "outage":
        assert set(docs(rows, "product:")) == {"product:2"}
        assert state["retry"] == ["product:3", "product:4", "product:5"]
    else:
        assert set(docs(rows, "product:")) == {"product:2", "product:3"}
        assert state["retry"] == []
    assert not docs(rows, "collection:")  # stopped before the collections

    # Next run, nothing changed upstream: the rest arrives, and product 1 goes.
    rows = run_sync(store, state, client=client)
    rest = (
        {"product:3", "product:4", "product:5"}
        if failure == "outage"
        else {"product:4", "product:5"}
    )
    assert set(docs(rows, "product:")) == rest
    assert all("v2" in row["content"] for row in docs(rows, "product:").values())
    assert tombstones(rows) == ["product:1"]
    assert state["retry"] == []


def test_401_before_anything_was_rendered_raises(store):
    state = _five_products(store)
    before = copy.deepcopy(state)
    store.queue("Product", _Resp(401, {"errors": "Invalid"}), when=_on(1))

    with pytest.raises(ShopifyAPIError) as excinfo:
        run_sync(store, state)
    assert excinfo.value.status == 401
    assert state == before


def test_rate_limit_while_listing_stops_cleanly_with_nothing_moved(store):
    state = {}
    run_sync(store, state)
    before = copy.deepcopy(state)
    store.destroy("Product", 2)
    store.queue("ProductSweep", _Resp(429, {}, {"Retry-After": "600"}))
    stats = {}

    assert run_sync(store, state, stats=stats) == []
    assert stats["failed_rate_limit"] == 1
    assert state == before


@pytest.mark.parametrize("refusal", ["403", "null"])
def test_three_unreadable_items_never_block_the_run_and_are_given_up(store, refusal):
    for n in (4, 5):
        store.add_product(n)
    state = {}
    run_sync(store, state)
    for n in (1, 2, 3, 4, 5):
        store.products[gid("Product", n)]["updatedAt"] = at(5)

    def refuse():
        for n in (2, 3, 4):
            if refusal == "403":  # counted: products 1 and 5 are read in the same run
                response = _Resp(403, {"errors": "Forbidden"})
            else:
                response = _Resp(200, {"data": {"product": None}})
            store.queue("Product", response, when=_on(n))

    for run in (1, 2):
        refuse()
        # Another product is read in the same run, so a 403 counts toward give-up.
        store.products[gid("Product", 1)]["updatedAt"] = at(10 + run)
        stats = {}
        rows = run_sync(store, state, stats=stats)
        if run == 1:
            assert set(docs(rows, "product:")) == {"product:1", "product:5"}
        assert tombstones(rows) == []
        assert state["retry"] == ["product:2", "product:3", "product:4"]
        runs = {k: v["runs"] for k, v in state["unreadable"].items()}
        assert runs == {"product:2": run, "product:3": run, "product:4": run}
        assert stats["kept"] == 3 and "failed_unavailable" not in stats

    store.add_product(6, updated_at=at(6))  # created meanwhile: still ingested
    refuse()
    stats = {}
    rows = run_sync(store, state, stats=stats)
    assert "product:6" in docs(rows)
    assert tombstones(rows) == ["product:2", "product:3", "product:4"]
    assert stats["given_up"] == 3 and state["retry"] == [] and state["unreadable"] == {}
    run_sync(store, state)
    assert store.fetched("Product") == []  # no longer asked about

    store.products[gid("Product", 3)]["updatedAt"] = at(7)  # it changed: asked again
    rows = run_sync(store, state)
    assert set(docs(rows, "product:")) == {"product:3"}
    assert "product:3" not in state["given_up"]


def test_access_denied_on_one_item_aborts_with_the_field_access_error(store):
    state = _five_products(store)
    before = copy.deepcopy(state)
    message = "This app is not approved to use the vendor field."
    denied = {"message": message, "extensions": {"code": "ACCESS_DENIED"}}
    store.queue("Product", _Resp(200, {"errors": [denied]}), when=_on(2))

    rows = []
    with pytest.raises(ShopifyAPIError) as excinfo:
        for row in run_sync_iter(store, state):
            rows.append(row)
    assert "the app may not read the vendor field" in str(excinfo.value)
    assert excinfo.value.code == "ACCESS_DENIED"
    assert tombstones(rows) == [] and state == before


def test_access_denied_on_a_listing_aborts_with_the_field_access_error(store):
    denied = {"message": "Access denied", "extensions": {"code": "ACCESS_DENIED"}}
    store.queue("ProductSweep", _Resp(200, {"errors": [denied]}))
    state = {}
    with pytest.raises(ShopifyAPIError, match="may not read a field this query asks for"):
        run_sync(store, state)
    assert state == {}


def test_access_denied_on_a_deletion_check_aborts(store):
    state = {}
    run_sync(store, state)
    before = copy.deepcopy(state)
    store.destroy("Product", 2)
    store.events.clear()  # no event: nodes() is asked
    denied = {"message": "Access denied", "extensions": {"code": "ACCESS_DENIED"}}
    store.queue("Nodes", _Resp(200, {"errors": [denied]}))

    with pytest.raises(ShopifyAPIError, match="may not read"):
        run_sync(store, state)
    assert state == before


def test_a_kind_whose_every_fetch_answers_403_never_moves_toward_give_up(store, caplog):
    state = {}
    run_sync(store, state)
    for run in range(4):
        for n in (1, 2, 3):
            store.products[gid("Product", n)]["updatedAt"] = at(5 + run)
            store.queue("Product", _Resp(403, {"errors": "Forbidden"}), when=_on(n))
        stats = {}
        rows = run_sync(store, state, stats=stats)
        assert tombstones(rows) == []
        assert stats["failed_permission"] == 3
        assert state["unreadable"] == {}  # runs never moves
        assert state["retry"] == ["product:1", "product:2", "product:3"]
    assert "product: every fetch answered 403 this run" in caplog.text
    assert {"product:1", "product:2", "product:3"} <= set(state["known_ids"])


def test_missing_id_that_keeps_resolving_is_given_up_after_three_runs(store):
    state = {}
    run_sync(store, state)
    store.unlisted.add(gid("Product", 2))  # out of the sweep, yet nodes() finds it

    for _ in range(2):
        stats = {}
        assert tombstones(run_sync(store, state, stats=stats)) == []
        assert stats["kept"] == 1
    stats = {}
    assert tombstones(run_sync(store, state, stats=stats)) == ["product:2"]
    assert stats["given_up"] == 1
    assert tombstones(run_sync(store, state)) == []


def test_retries_go_last_and_never_count_toward_an_outage(store):
    for n in (4, 5, 6, 7):
        store.add_product(n)
    state = {}
    run_sync(store, state)
    for n in (2, 3, 4, 5, 6):
        store.products[gid("Product", n)]["updatedAt"] = at(5)
    for n in (2, 4, 6):  # kept after a 500, with successes between them
        _fail(store, "Product", n)
    run_sync(store, state)
    assert state["retry"] == ["product:2", "product:4", "product:6"]

    store.products[gid("Product", 7)]["updatedAt"] = at(6)
    for n in (2, 4, 6):  # still failing, three in a row at the end
        _fail(store, "Product", n)
    stats = {}
    rows = run_sync(store, state, stats=stats)

    order = list(dict.fromkeys(v["id"] for v in store.ops("Product")))
    assert order == [gid("Product", n) for n in (7, 2, 4, 6)]
    assert set(docs(rows, "product:")) == {"product:7"}
    assert "failed_unavailable" not in stats and stats["failed"] == 3


def test_a_success_between_server_errors_resets_the_outage_streak(store):
    state = _five_products(store)
    for n in (1, 2, 4, 5):  # 3 succeeds in between
        _fail(store, "Product", n)
    stats = {}

    rows = run_sync(store, state, stats=stats)

    assert set(docs(rows, "product:")) == {"product:3"}
    assert "failed_unavailable" not in stats
    assert state["retry"] == ["product:1", "product:2", "product:4", "product:5"]


def test_item_body_of_the_wrong_shape_aborts(store):
    state = _five_products(store)
    before = copy.deepcopy(state)
    store.queue("Product", _Resp(200, {"data": {"product": ["not", "an", "object"]}}), when=_on(2))

    with pytest.raises(ShopifyResponseError):
        run_sync(store, state)
    assert state == before


# ---------------------------------------------------------------------------
# sync_store: customers and orders (opt-in)
# ---------------------------------------------------------------------------
ALL = ("products", "collections", "customers", "orders")


@pytest.fixture
def shop_with_orders(store):
    store.add_customer(
        5,
        displayName="Ada Lovelace",
        note="VIP",
        tags=["wholesale"],
        defaultAddress={
            "city": "London",
            "country": "United Kingdom",
            "address1": "12 Secret Street",
        },
        phone="+44 1",
    )
    store.add_order(6, customer=5, products=[1, 2])
    store.add_order(7, products=[3], updated_at=at(2))
    return store


def test_customers_and_orders_when_asked(shop_with_orders):
    rows = run_sync(shop_with_orders, {}, resources=ALL)

    customer = docs(rows)["customer:5"]["content"]
    assert customer == (
        "Type: customer\nEmail: c5@example.com\nState: enabled\n"
        "Location: London, United Kingdom\nTags: wholesale\n\nVIP"
    )
    assert "Secret Street" not in customer and "+44" not in customer
    order = docs(rows)["order:6"]["content"]
    assert "Customer: Ada Lovelace (customer:5)" in order
    assert "- 1 x Linen shirt (product:1)" in order and "- 1 x Wool sweater (product:2)" in order


def test_renamed_customer_or_product_re_renders_unlisted_orders(shop_with_orders):
    state = {}
    run_sync(shop_with_orders, state, resources=ALL)
    shop_with_orders.customers[gid("Customer", 5)].update(displayName="Ada King", updatedAt=at(5))
    shop_with_orders.products[gid("Product", 3)].update(title="Canvas bag", updatedAt=at(5))

    rows = run_sync(shop_with_orders, state, resources=ALL)

    assert {"order:6", "order:7"} <= set(docs(rows))
    assert "Customer: Ada King (customer:5)" in docs(rows)["order:6"]["content"]
    assert "- 1 x Canvas bag (product:3)" in docs(rows)["order:7"]["content"]
    # Order 6 is outside the listing's overlap, so it is fetched because a name it
    # shows changed; order 7 (updated at the cursor) is listed again anyway.
    assert shop_with_orders.fetched("Order") == [6]


def test_order_ageing_out_of_the_window_is_not_deleted(shop_with_orders):
    state = {}
    run_sync(shop_with_orders, state, resources=ALL)
    # Two months later order 6 (created October 1) is past the window: no longer
    # listed, and assumed to answer null by id, but that is not a deletion.
    del shop_with_orders.orders[gid("Order", 6)]

    rows = run_sync(shop_with_orders, state, resources=ALL, now="2026-12-17T12:00:00Z")
    assert tombstones(rows) == []
    assert "order:6" in state["known_ids"]


def test_order_near_the_edge_of_the_window_is_not_asked_about(shop_with_orders):
    state = {}
    run_sync(shop_with_orders, state, resources=ALL)
    del shop_with_orders.orders[gid("Order", 6)]

    # 54 days after its creation (October 1): inside the window, but within the
    # 7-day margin where a null answer is not trusted as a deletion.
    rows = run_sync(shop_with_orders, state, resources=ALL, now="2026-11-24T10:00:00Z")
    assert tombstones(rows) == []
    assert "order:6" in state["known_ids"]
    assert all(gid("Order", 6) not in v["ids"] for v in shop_with_orders.ops("Nodes"))


def _sync_on_shop_clock(fake, state):
    """A run as shopify_source makes it: no ``now`` given, so the window uses Shopify's clock."""
    fake.calls.clear()
    return list(sync_store(client_for(fake), state, resources=ALL))


def test_order_window_is_measured_on_shopifys_clock(shop_with_orders, monkeypatch):
    state = {}
    run_sync(shop_with_orders, state, resources=ALL)
    del shop_with_orders.orders[gid("Order", 6)]
    shop_with_orders.deleted.add(gid("Order", 6))
    # The local clock says October 17 (order 6 well inside the window, would be
    # asked about and forgotten); Shopify's Date header says November 24 (inside
    # the margin). Shopify's clock wins.
    monkeypatch.setattr(shopify, "_local_now", lambda: shopify.datetime.fromisoformat(NOW))
    shop_with_orders.clock = "2026-11-24T10:00:00Z"

    assert tombstones(_sync_on_shop_clock(shop_with_orders, state)) == []
    assert "order:6" in state["known_ids"]

    shop_with_orders.clock = NOW  # Shopify's clock agrees with the local one now
    assert tombstones(_sync_on_shop_clock(shop_with_orders, state)) == ["order:6"]


def test_without_a_date_header_the_local_clock_is_used_and_logged(
    shop_with_orders, monkeypatch, caplog
):
    state = {}
    run_sync(shop_with_orders, state, resources=ALL)
    del shop_with_orders.orders[gid("Order", 6)]
    shop_with_orders.deleted.add(gid("Order", 6))
    monkeypatch.setattr(shopify, "_local_now", lambda: shopify.datetime.fromisoformat(NOW))
    shop_with_orders.clock = None

    assert tombstones(_sync_on_shop_clock(shop_with_orders, state)) == ["order:6"]
    assert "no Date header" in caplog.text


def test_destroyed_order_is_forgotten(shop_with_orders):
    state = {}
    run_sync(shop_with_orders, state, resources=ALL)
    shop_with_orders.destroy("Order", 6)

    assert tombstones(run_sync(shop_with_orders, state, resources=ALL)) == ["order:6"]


def test_deleted_order_without_a_destroy_event_is_forgotten(shop_with_orders):
    """Deleting an order in the admin leaves no destroy event (seen live); nodes() says null."""
    state = {}
    run_sync(shop_with_orders, state, resources=ALL)
    del shop_with_orders.orders[gid("Order", 6)]
    shop_with_orders.deleted.add(gid("Order", 6))

    rows = run_sync(shop_with_orders, state, resources=ALL)
    assert tombstones(rows) == ["order:6"]
    assert "order:6" not in state["known_ids"] and "order:6" not in state["orders"]
    # Only the order not listed this run is asked about (order 7 is listed again).
    assert [v["ids"] for v in shop_with_orders.ops("Nodes")] == [[gid("Order", 6)]]


def test_unlisted_orders_that_still_resolve_are_kept_quietly(shop_with_orders, caplog):
    state = {}
    run_sync(shop_with_orders, state, resources=ALL)

    rows = run_sync(shop_with_orders, state, resources=ALL)
    assert tombstones(rows) == []
    assert {"order:6", "order:7"} <= set(state["known_ids"])
    assert "kept" not in caplog.text.replace("kept 0 item(s)", "")


def test_order_check_that_fails_keeps_the_order(shop_with_orders):
    state = {}
    run_sync(shop_with_orders, state, resources=ALL)
    del shop_with_orders.orders[gid("Order", 6)]
    shop_with_orders.deleted.add(gid("Order", 6))
    for _ in range(shopify._MAX_RETRIES):
        shop_with_orders.queue("Nodes", _Resp(500, {"errors": "boom"}))

    assert tombstones(run_sync(shop_with_orders, state, resources=ALL)) == []
    assert "order:6" in state["known_ids"]
    assert tombstones(run_sync(shop_with_orders, state, resources=ALL)) == ["order:6"]


def test_name_references_are_kept_only_inside_the_order_window(shop_with_orders):
    shop_with_orders.add_order(
        8, products=[1], created_at="2026-07-01T00:00:00Z", updated_at="2026-07-01T00:00:00Z"
    )
    state = {}
    run_sync(shop_with_orders, state, resources=ALL)
    assert "order:8" not in state["orders"] and "order:6" in state["orders"]


def test_orders_without_customers_name_the_customer_by_id(shop_with_orders):
    rows = run_sync(shop_with_orders, {}, resources=("products", "orders"))
    assert "Customer: customer:5\n" in docs(rows)["order:6"]["content"] + "\n"
    assert not shop_with_orders.ops("CustomerSweep")


def test_customers_the_app_may_not_read_stop_the_run_with_a_clear_error(store):
    # Seen live: "This app is not approved to access the Customer object."
    store.queue(
        "CustomerSweep",
        _Resp(
            200,
            {
                "errors": [
                    {
                        "message": "This app is not approved to access the Customer object.",
                        "extensions": {"code": "ACCESS_DENIED"},
                    }
                ]
            },
        ),
    )
    state = {}
    with pytest.raises(ShopifyAPIError, match="protected customer data") as excinfo:
        run_sync(store, state, resources=ALL)
    assert "with the Name and Email fields" in str(excinfo.value)
    assert state == {}


def test_deselecting_a_resource_forgets_its_documents(shop_with_orders):
    state = {}
    run_sync(shop_with_orders, state, resources=ALL)
    rows = run_sync(shop_with_orders, state)
    assert tombstones(rows) == ["customer:5", "order:6", "order:7"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_client_credentials_fetch_a_token_and_refresh_once_on_401(store):
    client = client_for(store, token="grant")
    state = {}
    run_sync(store, state, client=client)
    assert store.grants == 1

    store.expire()  # the 24 h token ran out mid-life
    for n in (1, 2):
        store.products[gid("Product", n)]["updatedAt"] = at(5)
    rows = run_sync(store, state, client=client)
    assert store.grants == 2
    assert set(docs(rows, "product:")) == {"product:1", "product:2"}


def test_second_401_after_a_refresh_aborts_with_state_unchanged(store):
    client = client_for(store, token="grant")
    state = {}
    run_sync(store, state, client=client)
    before = copy.deepcopy(state)
    store.queue("ProductSweep", _Resp(401, {"errors": "Invalid"}))
    store.queue("ProductSweep", _Resp(401, {"errors": "Invalid"}))

    with pytest.raises(ShopifyAPIError) as excinfo:
        run_sync(store, state, client=client)
    assert excinfo.value.status == 401
    assert store.grants == 2  # refreshed exactly once
    assert state == before


def test_static_token_is_never_refreshed(store):
    state = {}
    run_sync(store, state)
    store.expire()
    with pytest.raises(ShopifyAPIError, match="401"):
        run_sync(store, state, client=_Client(store, SHOP, "shpat_static", None, None))
    assert store.grants == 0


def test_refused_grant_says_why_without_the_secret(store):
    client = _Client(store, SHOP, None, "app-id", "wrong-secret")
    with pytest.raises(ShopifyAPIError) as excinfo:
        run_sync(store, {}, client=client)
    assert str(excinfo.value) == "Shopify oauth/access_token: HTTP 400"
    assert excinfo.value.detail == "invalid_client"
    assert "wrong-secret" not in str(excinfo.value)


def test_token_never_reaches_state_or_logs(store, caplog):
    caplog.set_level(logging.DEBUG)
    client = client_for(store, token="grant")
    state = {}
    run_sync(store, state, client=client)
    store.expire()
    store.products[gid("Product", 1)]["updatedAt"] = at(5)
    run_sync(store, state, client=client)

    dumped = json.dumps(state)
    logs = caplog.text + " ".join(str(r.args) for r in caplog.records)
    for secret in ("shpat_issued_1", "shpat_issued_2", CLIENT_SECRET, "app-id"):
        assert secret not in dumped
        assert secret not in logs


# ---------------------------------------------------------------------------
# shopify_source: validation and dlt wiring
# ---------------------------------------------------------------------------
def _source(fake, **kwargs):
    fake.tokens.add("shpat_static")
    return shopify_source(SHOP, access_token="shpat_static", session=fake, **kwargs)


def test_source_is_configured_for_merge_and_hard_delete(fake):
    resource = _source(fake)
    assert resource.name == "shopify_documents"
    schema = resource.compute_table_schema()
    disposition = schema.get("write_disposition")
    if isinstance(disposition, dict):
        disposition = disposition.get("disposition")
    assert disposition == "merge"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True
    assert document_source_tag(resource) == "shopify"


def test_resource_name_names_the_table_and_scopes_the_state(fake):
    default = _source(fake)
    other = _source(fake, resource_name="shopify_eu")
    assert other.name == "shopify_eu"
    assert pipeline_name_for_source(other, "s") != pipeline_name_for_source(default, "s")


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={"id": "product:1", "url": "", "title": "Linen shirt", "content": "Type: product"},
        content_hash="abc",
        table_name="shopify_documents",
    )
    item = _build_document_data_item(row, uuid5(NAMESPACE_OID, "product:1"), "shopify")
    assert item.system_metadata["source"] == "shopify"
    assert item.data == "# Linen shirt\n\nType: product"


@pytest.mark.parametrize("kwargs", [{"resources": ["inventory"]}, {"resources": []}])
def test_source_rejects_unknown_resources(fake, kwargs):
    with pytest.raises(ValueError, match="resources"):
        _source(fake, **kwargs)


def test_source_requires_credentials(monkeypatch):
    for name in ("SHOPIFY_ACCESS_TOKEN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="SHOPIFY_CLIENT_ID"):
        shopify_source(SHOP)


def test_source_reads_shop_and_credentials_from_env(monkeypatch):
    monkeypatch.setenv("SHOPIFY_SHOP", "acme")
    monkeypatch.setenv("SHOPIFY_CLIENT_ID", "app-id")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.delenv("SHOPIFY_ACCESS_TOKEN", raising=False)
    assert shopify_source().name == "shopify_documents"  # no request is made yet


# ---------------------------------------------------------------------------
# dlt pipeline: a real merge into a temp sqlite destination
# ---------------------------------------------------------------------------
def _pipeline(tmp_path, name):
    import dlt

    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'shop.db').as_posix()}"),
        dataset_name="shop_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _run_pipeline(tmp_path, fake, **kwargs):
    fake.calls.clear()
    source = _source(fake, **kwargs)
    pipeline = _pipeline(tmp_path, pipeline_name_for_source(source, "shop_ds"))
    pipeline.run(source)
    return pipeline


def _read(pipeline, table="shopify_documents"):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, content FROM {table}") as cursor,
    ):
        return dict(cursor.fetchall())


def _scope():
    return pipeline_name_for_source(_source(FakeShopify()), "shop_ds")


def _chain(error):
    chain = []
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    return chain


def test_pipeline_round_trip_edit_and_delete(store, tmp_path):
    assert set(_read(_run_pipeline(tmp_path, store))) == {
        "product:1",
        "product:2",
        "product:3",
        "collection:10",
        "collection:11",
    }

    store.products[gid("Product", 1)].update(description="v2", updatedAt=at(5))
    store.destroy("Product", 2)
    documents = _read(_run_pipeline(tmp_path, store))

    assert store.fetched("Product") == [1]
    assert set(documents) == {"product:1", "product:3", "collection:10", "collection:11"}
    assert "v2" in documents["product:1"]
    assert "Wool sweater" not in documents["collection:10"]


def test_pipeline_deletion_removes_the_row_from_the_destination(store, tmp_path):
    # Goes red if the _deleted marker is not emitted or hard_delete is off.
    _run_pipeline(tmp_path, store)
    store.destroy("Product", 3)
    pipeline = _run_pipeline(tmp_path, store)
    with (
        pipeline.sql_client() as client,
        client.execute_query(
            "SELECT COUNT(*) FROM shopify_documents WHERE id = 'product:3'"
        ) as cursor,
    ):
        assert cursor.fetchone()[0] == 0


def test_pipeline_two_sources_in_one_dataset_stay_apart(store, tmp_path):
    one = {"resources": ["products"], "resource_name": "shopify_one"}
    two = {"resources": ["collections"], "resource_name": "shopify_two"}
    _run_pipeline(tmp_path, store, **one)
    _run_pipeline(tmp_path, store, **two)
    first = _read(_run_pipeline(tmp_path, store, **one), "shopify_one")
    second = _read(_run_pipeline(tmp_path, store, **two), "shopify_two")
    assert set(first) == {"product:1", "product:2", "product:3"}
    assert set(second) == {"collection:10", "collection:11"}


def test_check_active_is_asked_before_every_request(store):
    counted = []
    source = _source(store, check_active=lambda: counted.append(len(store.calls)))
    assert list(source)
    assert sorted(set(counted)) == list(range(len(store.calls) + 1))


@pytest.mark.parametrize("stop_after", [1, 3, 5])  # sweep, events, mid-render
def test_check_active_raising_stops_with_nothing_loaded(store, tmp_path, stop_after):
    _run_pipeline(tmp_path, store)
    before = _read(_pipeline(tmp_path, _scope()))
    store.destroy("Product", 2)
    store.products[gid("Product", 1)].update(description="v2", updatedAt=at(5))

    class AccessRevokedError(Exception):
        pass

    def check_active():
        if len(store.calls) >= stop_after:
            raise AccessRevokedError

    with pytest.raises(PipelineStepFailed) as excinfo:
        _run_pipeline(tmp_path, store, check_active=check_active)
    assert any(isinstance(e, AccessRevokedError) for e in _chain(excinfo.value))
    assert _read(_pipeline(tmp_path, _scope())) == before
    documents = _read(_run_pipeline(tmp_path, store))
    assert "product:2" not in documents and "v2" in documents["product:1"]


def test_check_active_raising_an_oserror_is_not_retried_or_swallowed(store, tmp_path, sleeps):
    _run_pipeline(tmp_path, store)
    before = _read(_pipeline(tmp_path, _scope()))
    store.products[gid("Product", 1)].update(description="v2", updatedAt=at(5))

    def check_active():
        if store.ops("Product"):
            raise PermissionError("access revoked")

    with pytest.raises(PipelineStepFailed) as excinfo:
        _run_pipeline(tmp_path, store, check_active=check_active)
    # PermissionError is an OSError; only requests' own errors are retried.
    assert any(isinstance(e, PermissionError) for e in _chain(excinfo.value))
    assert sleeps == []
    assert _read(_pipeline(tmp_path, _scope())) == before


def test_pipeline_product_that_fails_on_its_own_is_kept_and_retried(store, tmp_path):
    old = _read(_run_pipeline(tmp_path, store))["product:2"]
    for n in (1, 2):
        store.products[gid("Product", n)].update(description="v2", updatedAt=at(5))
    _fail(store, "Product", 2)
    documents = _read(_run_pipeline(tmp_path, store))
    assert "v2" in documents["product:1"] and documents["product:2"] == old

    documents = _read(_run_pipeline(tmp_path, store))
    assert store.fetched("Product") == [2] and "v2" in documents["product:2"]


def test_pipeline_401_mid_run_commits_progress_and_resumes(store, tmp_path):
    # A token refused on product 3 of 5 (after the one refresh): 1-2 are kept,
    # 3-5 come next run with nothing changed upstream, and deletions wait.
    for n in (4, 5, 6):
        store.add_product(n)

    def source():
        return shopify_source(SHOP, client_id="app-id", client_secret=CLIENT_SECRET, service=store)

    def run():
        store.calls.clear()
        src = source()
        pipeline = _pipeline(tmp_path, pipeline_name_for_source(src, "shop_ds"))
        pipeline.run(src)
        return src, _read(pipeline)

    run()
    for n in (1, 2, 3, 4, 5):
        store.products[gid("Product", n)].update(description="v2", updatedAt=at(5))
    store.destroy("Product", 6)
    store.queue("Product", _Resp(401, {"errors": "Invalid"}), when=_on(3))
    store.queue("Product", _Resp(401, {"errors": "Invalid"}), when=_on(3))

    src, documents = run()
    assert src.cognee_sync_stats["failed_auth"] == 1
    assert "v2" in documents["product:1"] and "v2" in documents["product:2"]
    assert "v2" not in documents["product:3"]
    assert "product:6" in documents  # no deletion on a stopped run

    src, documents = run()
    assert store.fetched("Product") == [3, 4, 5]
    assert all("v2" in documents[f"product:{n}"] for n in (1, 2, 3, 4, 5))
    assert "product:6" not in documents


# ---------------------------------------------------------------------------
# Review round 2: secrets, waits, wiring
# ---------------------------------------------------------------------------
def test_token_with_a_trailing_newline_is_refused_without_echoing_it():
    for kwargs in (
        {"access_token": "shpat_SECRET\n"},
        {"client_id": "app-id", "client_secret": "shpss_SECRET\n"},
    ):
        with pytest.raises(ValueError) as excinfo:
            shopify_source(SHOP, **kwargs)
        assert "SECRET" not in str(excinfo.value) and "whitespace" in str(excinfo.value)


def test_the_token_is_never_in_an_error_message_or_a_log_line(store, caplog, sleeps):
    caplog.set_level(logging.DEBUG)
    secret = "shpat_static"

    class Leaky(FakeShopify):
        def post(self, url, json=None, data=None, headers=None, timeout=None):
            if json and "ProductSweep" in json["query"]:
                raise requests.exceptions.InvalidHeader(f"Invalid header value b'{secret}'")
            return super().post(url, json=json, data=data, headers=headers, timeout=timeout)

    leaky = Leaky()
    with pytest.raises(ShopifyAPIError) as excinfo:
        run_sync(leaky, {})
    assert secret not in str(excinfo.value) and excinfo.value.status == 0

    # A 401 whose body echoes the token: neither the message nor the logs carry it.
    store.queue("ProductSweep", _Resp(401, {"errors": f"bad token {secret}"}))
    with pytest.raises(ShopifyAPIError) as excinfo:
        run_sync(store, {})
    assert str(excinfo.value) == "Shopify ProductSweep: HTTP 401"
    assert secret not in str(excinfo.value) and secret not in excinfo.value.detail
    assert secret not in caplog.text + " ".join(str(r.args) for r in caplog.records)
    assert "InvalidHeader" in caplog.text  # the retries were logged by type


def test_waits_are_capped_and_ask_check_active_first(fake, sleeps):
    checks = []
    fake.tokens.add("shpat_static")
    client = _Client(fake, SHOP, "shpat_static", None, None, lambda: checks.append(len(sleeps)))
    fake.queue("ProductSweep", _Resp(503, {}, {"Retry-After": "30"}))
    fake.queue("ProductSweep", _Resp(429, {}, {"Retry-After": "61"}))

    with pytest.raises(ShopifyAPIError) as excinfo:
        shopify._list_all(client, shopify._PRODUCT_SWEEP, "products")
    assert excinfo.value.status == 429 and excinfo.value.rate_limited
    assert sleeps == [30.0]  # the 61 s wait was not taken
    assert checks == [0, 0, 1]  # before the request, before the wait, before the retry


@pytest.mark.parametrize("name", ["", "Foo-Bar", "a" * 65, None])
def test_resource_name_is_validated(fake, name):
    with pytest.raises(ValueError, match="resource_name"):
        _source(fake, resource_name=name)


def test_service_is_the_injection_kwarg_and_session_its_alias(store):
    store.tokens.add("shpat_static")
    assert list(shopify_source(SHOP, access_token="shpat_static", service=store))
    assert list(shopify_source(SHOP, access_token="shpat_static", session=store))
    with pytest.raises(ValueError, match="not both"):
        shopify_source(SHOP, access_token="shpat_static", service=store, session=store)


def test_session_is_built_per_run_and_closed(store, monkeypatch):
    built = []

    class Closing(FakeShopify):
        closed = False

        def close(self):
            self.closed = True

    def make_session():
        session = Closing()
        session.__dict__.update(copy.deepcopy(store.__dict__))
        session.tokens.add("shpat_static")
        built.append(session)
        return session

    monkeypatch.setattr(shopify, "_make_session", make_session)
    source = shopify_source(SHOP, access_token="shpat_static")
    assert built == []  # nothing is opened before the run
    assert list(source)
    assert len(built) == 1 and built[0].closed


def test_sync_stats_count_what_a_run_did(store, tmp_path):
    def run():
        source = _source(store)
        _pipeline(tmp_path, pipeline_name_for_source(source, "shop_ds")).run(source)
        return source.cognee_sync_stats

    assert run() == {
        "scanned": 5,
        "skipped": 0,
        "failed": 0,
        "deleted": 0,
        "kept": 0,
        "given_up": 0,
    }
    store.destroy("Product", 3)
    assert run()["deleted"] == 1
