"""In-memory Stripe API stand-in for the Stripe connector tests.

``FakeStripe`` is mounted on a real ``requests.Session`` as a transport
adapter and handed to the official client through ``stripe.RequestsClient``,
so the connector's whole HTTP stack runs unchanged: request signing, the
``Stripe-Account`` and ``Stripe-Version`` headers, list paging and error
mapping. Only the network is replaced.

It mirrors the Stripe behaviour the connector relies on:

* lists are newest first, paged with ``limit`` + ``starting_after``, and
  filter on ``created[gte]``; subscriptions leave canceled ones out unless
  ``status=all``;
* every change writes an event whose ``data.object`` is the object as it
  was; events older than the retention window are gone;
* a deleted customer stays retrievable as a ``deleted: true`` stub, a deleted
  product or draft invoice answers 404, and a canceled subscription stays
  listed with ``status=all``.
"""

import json
from urllib.parse import parse_qsl, urlsplit

import requests
import stripe
from requests.adapters import BaseAdapter

API_HOST = "api.stripe.com"
PLATFORM_KEY = "sk_test_platform"
ACCOUNT_ID = "acct_connected"

# Unix time of 2026-01-01T00:00:00Z.
START = 1_767_225_600

_PREFIXES = {
    "customers": ("customer", "cus"),
    "subscriptions": ("subscription", "sub"),
    "invoices": ("invoice", "in"),
    "charges": ("charge", "ch"),
    "products": ("product", "prod"),
    "disputes": ("dispute", "dp"),
}
_EVENT_PREFIXES = {
    "customer": "customer",
    "subscription": "customer.subscription",
    "invoice": "invoice",
    "charge": "charge",
    "product": "product",
    "dispute": "charge.dispute",
}


class FakeStripe(BaseAdapter):
    """Serves the Stripe list, retrieve and events endpoints the connector reads."""

    def __init__(self, start: int = START):
        super().__init__()
        self.objects: dict[str, dict[str, dict]] = {name: {} for name in _PREFIXES}
        self.deleted_customers: set[str] = set()
        self.events: list[dict] = []
        # (method, path, query, headers) of every request served.
        self.requests: list[tuple[str, str, dict, dict]] = []
        # (path substring, status): served once instead of the real response.
        self.failures: list[tuple[str, int]] = []
        self.clock = start
        self._next_id = 1

    # -- client wiring -----------------------------------------------------

    def http_client(self) -> stripe.RequestsClient:
        session = requests.Session()
        session.mount(f"https://{API_HOST}/", self)
        return stripe.RequestsClient(session=session)

    # -- fixture builders --------------------------------------------------

    def tick(self, seconds: int = 1) -> int:
        self.clock += seconds
        return self.clock

    def add(self, resource: str, object_id: str | None = None, **fields) -> dict:
        object_name, prefix = _PREFIXES[resource]
        if object_id is None:
            object_id = f"{prefix}_{self._next_id}"
            self._next_id += 1
        obj = {
            "id": object_id,
            "object": object_name,
            "created": fields.pop("created", None) or self.tick(),
            "livemode": False,
            **fields,
        }
        self.objects[resource][object_id] = obj
        self.event(f"{_EVENT_PREFIXES[object_name]}.created", obj)
        return obj

    def update(self, resource: str, object_id: str, **fields) -> dict:
        obj = self.objects[resource][object_id]
        obj.update(fields)
        self.tick()
        self.event(f"{_EVENT_PREFIXES[obj['object']]}.updated", obj)
        return obj

    def delete(self, resource: str, object_id: str) -> None:
        obj = self.objects[resource][object_id]
        self.tick()
        if resource == "subscriptions":
            # Canceling is the only "delete" a subscription has; it stays listed.
            obj.update(status="canceled", canceled_at=self.clock, ended_at=self.clock)
        else:
            del self.objects[resource][object_id]
            if resource == "customers":
                self.deleted_customers.add(object_id)
        self.event(f"{_EVENT_PREFIXES[obj['object']]}.deleted", obj)

    def event(self, event_type: str, obj: dict, created: int | None = None) -> None:
        self.events.append(
            {
                "id": f"evt_{len(self.events) + 1}",
                "object": "event",
                "type": event_type,
                "created": created or self.clock,
                "livemode": False,
                "data": {"object": json.loads(json.dumps(obj))},
            }
        )

    def expire_events_before(self, timestamp: int) -> None:
        """Drop events older than ``timestamp``, as Stripe's 30-day retention does."""
        self.events = [event for event in self.events if event["created"] >= timestamp]

    # -- request log helpers -----------------------------------------------

    def paths(self, method: str = "GET") -> list[str]:
        return [path for verb, path, _query, _headers in self.requests if verb == method]

    def clear_requests(self) -> None:
        self.requests.clear()

    # -- transport ---------------------------------------------------------

    def send(self, request, **_kwargs):
        url = urlsplit(request.url)
        query = dict(parse_qsl(url.query))
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.requests.append((request.method, url.path, query, headers))

        for index, (needle, status) in enumerate(self.failures):
            if needle in url.path:
                del self.failures[index]
                return _response(request, status, _error("api_error", "Injected failure."))

        if headers.get("authorization") != f"Bearer {PLATFORM_KEY}":
            return _response(request, 401, _error("invalid_request_error", "Invalid API Key."))
        if headers.get("stripe-account") != ACCOUNT_ID:
            return _response(request, 403, _error("invalid_request_error", "No such account."))

        parts = url.path.strip("/").split("/")
        if request.method != "GET" or len(parts) not in (2, 3) or parts[0] != "v1":
            return _response(request, 404, _error("invalid_request_error", "Unrecognized URL."))
        resource = parts[1]
        if len(parts) == 2 and resource == "events":
            return _response(request, 200, self._list(url.path, self.events, query))
        if resource not in self.objects:
            return _response(request, 404, _error("invalid_request_error", "Unrecognized URL."))
        if len(parts) == 3:
            return self._retrieve(request, resource, parts[2])
        objects = list(self.objects[resource].values())
        if resource == "subscriptions" and query.get("status") != "all":
            objects = [obj for obj in objects if obj.get("status") != "canceled"]
        return _response(request, 200, self._list(url.path, objects, query))

    def close(self):
        pass

    def _retrieve(self, request, resource: str, object_id: str):
        obj = self.objects[resource].get(object_id)
        if obj is not None:
            return _response(request, 200, obj)
        if resource == "customers" and object_id in self.deleted_customers:
            return _response(request, 200, {"id": object_id, "object": "customer", "deleted": True})
        message = f"No such {resource}: '{object_id}'"
        return _response(request, 404, _error("invalid_request_error", message, "resource_missing"))

    @staticmethod
    def _list(path: str, objects: list[dict], query: dict) -> dict:
        if "created[gte]" in query:
            since = int(query["created[gte]"])
            objects = [obj for obj in objects if obj["created"] >= since]
        objects = sorted(objects, key=lambda obj: (obj["created"], obj["id"]), reverse=True)
        if "starting_after" in query:
            ids = [obj["id"] for obj in objects]
            objects = objects[ids.index(query["starting_after"]) + 1 :]
        limit = int(query.get("limit", 10))
        return {
            "object": "list",
            "url": path,
            "has_more": len(objects) > limit,
            "data": objects[:limit],
        }


def _error(error_type: str, message: str, code: str | None = None) -> dict:
    error = {"type": error_type, "message": message}
    if code:
        error["code"] = code
    return {"error": error}


def _response(request, status: int, body: dict) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response.headers["Content-Type"] = "application/json"
    response.headers["Request-Id"] = "req_fake"
    response.request = request
    response.url = request.url
    return response
