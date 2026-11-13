"""An in-memory stand-in for a ``requests`` session talking to a WooCommerce store.

Its behaviour mirrors what a real WooCommerce 11.2 store answered (WordPress 7.1,
both order storages): listings page by ``page`` / ``per_page`` with an
``X-WP-TotalPages`` header, ``modified_after`` is exclusive and read as GMT with
``dates_are_gmt=true``, ``status=any`` lists every order but trashed ones, a
trashed product's reviews answer ``post-trashed``, a deleted record answers 404,
and the order notes endpoint is not paged.
"""

from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from typing import Any

API = "http://shop.test/wp-json/wc/v3"
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def stamp(minutes: int) -> str:
    """A WooCommerce GMT timestamp ``minutes`` after ``T0``."""
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")


class Resp:
    def __init__(self, status: int, payload: Any, headers: dict | None = None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def error(status: int, code: str = "woocommerce_rest_error", headers: dict | None = None) -> Resp:
    return Resp(status, {"code": code, "message": "nope", "data": {"status": status}}, headers)


class FakeWooCommerce:
    """Products, reviews, orders (with notes) and customers, served like the REST API."""

    def __init__(self) -> None:
        self.products: dict[int, dict] = {}
        self.reviews: dict[int, dict] = {}
        self.orders: dict[int, dict] = {}
        self.notes: dict[int, list[dict]] = {}
        self.variations: dict[int, list[dict]] = {}
        self.customers: dict[int, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.kwargs: list[dict] = []
        # path regex -> list of Resp to answer (consumed in order) before the real handler
        self.failures: list[tuple[str, Resp]] = []
        self.drop_total_pages = False
        self.closed = False
        self.clock = 0

    # -- data ---------------------------------------------------------------
    def tick(self) -> str:
        self.clock += 1
        return stamp(self.clock)

    def add_product(self, pid: int, name: str, **fields: Any) -> dict:
        product = {
            "id": pid,
            "name": name,
            "status": "publish",
            "type": "simple",
            "sku": f"SKU-{pid}",
            "regular_price": "10",
            "sale_price": "",
            "price": "10",
            "categories": [{"id": 1, "name": "Things"}],
            "tags": [],
            "attributes": [],
            "short_description": "",
            "description": f"<p>About {name}.</p>",
            "permalink": f"http://shop.test/product/{pid}/",
            "variations": [],
            "stock_quantity": 5,
            "total_sales": 0,
            "average_rating": "0.00",
            "date_modified_gmt": self.tick(),
        }
        product.update(fields)
        self.products[pid] = product
        return product

    def edit_product(self, pid: int, **fields: Any) -> None:
        self.products[pid].update(fields, date_modified_gmt=self.tick())

    def add_review(self, rid: int, pid: int, text: str, rating: int = 4, **fields: Any) -> dict:
        review = {
            "id": rid,
            "product_id": pid,
            "product_name": self.products[pid]["name"] if pid in self.products else "",
            "product_permalink": f"http://shop.test/product/{pid}/",
            "status": "approved",
            "reviewer": "Asha",
            "reviewer_email": "asha@example.com",
            "review": f"<p>{text}</p>",
            "rating": rating,
            "verified": False,
            "date_created_gmt": self.tick(),
        }
        review.update(fields)
        self.reviews[rid] = review
        return review

    def add_order(self, oid: int, *, status: str = "processing", **fields: Any) -> dict:
        order = {
            "id": oid,
            "number": str(oid),
            "status": status,
            "currency": "USD",
            "total": "30.00",
            "discount_total": "0.00",
            "shipping_total": "0.00",
            "customer_id": 7,
            "customer_note": "",
            "payment_method_title": "Cash on delivery",
            "billing": {"email": "dana@example.com", "phone": "555-0100", "address_1": "1 Road"},
            "shipping": {"address_1": "1 Road"},
            "line_items": [{"name": "Mug", "quantity": 2, "total": "30.00", "sku": "MUG-1"}],
            "shipping_lines": [],
            "coupon_lines": [],
            "date_created_gmt": stamp(0),
            "date_modified_gmt": self.tick(),
        }
        order.update(fields)
        self.orders[oid] = order
        self.notes.setdefault(oid, [])
        return order

    def edit_order(self, oid: int, **fields: Any) -> None:
        self.orders[oid].update(fields, date_modified_gmt=self.tick())

    def add_note(self, oid: int, nid: int, text: str, *, to_customer: bool, author: str) -> None:
        # Like WooCommerce, a note leaves the order's date_modified alone.
        self.notes[oid].append(
            {
                "id": nid,
                "author": author,
                "note": text,
                "customer_note": to_customer,
                "date_created_gmt": self.tick(),
            }
        )

    def add_customer(self, cid: int, first: str, last: str, **fields: Any) -> dict:
        customer = {
            "id": cid,
            "first_name": first,
            "last_name": last,
            "email": f"{first.lower()}@example.com",
            "role": "customer",
            "billing": {"city": "Pune", "country": "IN", "company": "", "phone": "555-0101"},
            "date_created_gmt": stamp(0),
        }
        customer.update(fields)
        self.customers[cid] = customer
        return customer

    def fail(self, path_regex: str, *responses: Resp) -> None:
        """Answer the next requests to a matching path with ``responses``, in order."""
        self.failures.extend((path_regex, r) for r in responses)

    # -- session ------------------------------------------------------------
    def close(self) -> None:
        self.closed = True

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    def get(self, url: str, params: dict | None = None, **kwargs: Any) -> Resp:
        # The same store answers over http:// (OAuth 1.0a) and https:// (basic auth).
        base = API.split("://", 1)[1]
        assert url.split("://", 1)[1].startswith(base), f"unexpected URL {url}"
        path = url.split("://", 1)[1][len(base) :]
        params = dict(params or {})
        self.calls.append((path, params))
        self.kwargs.append(kwargs)
        for index, (pattern, response) in enumerate(self.failures):
            if re.fullmatch(pattern, path):
                del self.failures[index]
                return response
        return self._route(path, params)

    def _route(self, path: str, params: dict) -> Resp:
        if path == "/products":
            return self._listing(self._products(params), params)
        if path == "/products/reviews":
            return self._listing(self._reviews(params), params)
        if path == "/orders":
            return self._listing(self._orders(params), params)
        if path == "/customers":
            records = [c for c in self.customers.values() if c["role"] == params.get("role")]
            return self._listing(sorted(records, key=lambda r: r["id"]), params)
        match = re.fullmatch(r"/products/(\d+)/variations", path)
        if match:
            return self._listing(self.variations.get(int(match[1]), []), params)
        match = re.fullmatch(r"/orders/(\d+)/notes", path)
        if match:
            oid = int(match[1])
            if oid not in self.orders:
                return error(404, "woocommerce_rest_order_invalid_id")
            notes = self.notes[oid]
            if params.get("type") == "customer":
                notes = [n for n in notes if n["customer_note"]]
            return Resp(200, copy.deepcopy(list(reversed(notes))))
        for pattern, table in (
            (r"/products/reviews/(\d+)", self.reviews),
            (r"/products/(\d+)", self.products),
            (r"/orders/(\d+)", self.orders),
            (r"/customers/(\d+)", self.customers),
        ):
            match = re.fullmatch(pattern, path)
            if match:
                record = table.get(int(match[1]))
                if record is None:
                    return error(404, "woocommerce_rest_invalid_id")
                return Resp(200, copy.deepcopy(self._reviewed(record, table)))
        return error(404, "rest_no_route")

    def _reviewed(self, record: dict, table: dict) -> dict:
        if table is self.reviews:
            product = self.products.get(record["product_id"])
            if product is not None and product["status"] == "trash":
                return {**record, "status": "post-trashed"}
        return record

    def _products(self, params: dict) -> list[dict]:
        status = params.get("status")
        records = [
            p
            for p in self.products.values()
            if (status == "any" and p["status"] != "trash") or p["status"] == status
        ]
        return self._modified(records, params)

    def _reviews(self, params: dict) -> list[dict]:
        records = [self._reviewed(r, self.reviews) for r in self.reviews.values()]
        return sorted(
            (r for r in records if r["status"] == params.get("status")), key=lambda r: -r["id"]
        )

    def _orders(self, params: dict) -> list[dict]:
        assert params.get("status") == "any"
        return self._modified([o for o in self.orders.values() if o["status"] != "trash"], params)

    def _modified(self, records: list[dict], params: dict) -> list[dict]:
        after = params.get("modified_after")
        if after:
            assert params.get("dates_are_gmt") == "true"
            records = [r for r in records if r["date_modified_gmt"] > after]
        if params.get("orderby") == "modified":
            records = sorted(records, key=lambda r: (r["date_modified_gmt"], r["id"]))
        else:
            records = sorted(records, key=lambda r: r["id"])
        fields = params.get("_fields")
        if fields:
            keep = fields.split(",")
            records = [{k: r[k] for k in keep if k in r} for r in records]
        return records

    def _listing(self, records: list[dict], params: dict) -> Resp:
        per_page = int(params.get("per_page", 10))
        page = int(params.get("page", 1))
        total_pages = (len(records) + per_page - 1) // per_page
        chunk = records[(page - 1) * per_page : page * per_page]
        headers = {"X-WP-Total": str(len(records))}
        if not self.drop_total_pages:
            headers["X-WP-TotalPages"] = str(total_pages)
        return Resp(200, copy.deepcopy(chunk), headers)
