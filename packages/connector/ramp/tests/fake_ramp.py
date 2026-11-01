"""In-memory Ramp Developer API served through ``httpx.MockTransport``.

Shapes follow Ramp's OpenAPI spec and were checked against the sandbox (demo-api.ramp.com):
``{"data": [...], "page": {"next": "<BASE_URL>?<query with start>"}}`` listings,
``start`` = id of the previous page's last record, client-credentials tokens, and
``state`` filtering where DECLINED is hidden unless ``state=ALL``.
"""

import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from cognee_community_connector_ramp.ramp import RampClient, _iter_rows

BASE = "https://api.ramp.com"


class FakeRamp:
    def __init__(self):
        self.transactions: dict[str, dict] = {}
        self.receipts: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.tokens_issued = 0
        self.valid_token: str | None = None
        self.page_size_cap = 100
        # responses served before the normal ones, e.g. 429s or 500s
        self.queued: list[httpx.Response] = []
        self.credentials = ("client", "secret")

    # -- fixtures ------------------------------------------------------------

    def add(self, txn_id: str, when: str, *, memo=None, state="CLEARED", **fields) -> dict:
        transaction = {
            "id": txn_id,
            "user_transaction_time": when,
            "memo": memo,
            "state": state,
            "merchant_name": "Nopa",
            "merchant_location": {"city": "San Francisco", "state": "CA", "country": "US"},
            "sk_category_name": "Restaurants",
            "entity_amount": {"currency": "USD", "value": 48210},
            "minor_unit_conversion_rate": 100,
            "card_holder": {
                "first_name": "Sam",
                "last_name": "Lee",
                "department_id": fields.pop("department_id", "dep_eng"),
                "department_name": "Engineering",
                "user_id": fields.pop("user_id", "usr_sam"),
            },
            "attendees": [],
            "line_items": [],
            "policy_violations": [],
            "disputes": [],
            "accounting_field_selections": [],
            "receipts": [],
            "entity_id": "ent_1",
            **fields,
        }
        self.transactions[txn_id] = transaction
        return transaction

    def add_receipt(self, receipt_id: str, txn_id: str, created_at: str, items=("Burrata",)):
        receipt = {
            "id": receipt_id,
            "transaction_id": txn_id,
            "created_at": created_at,
            "user_id": "usr_sam",
            "receipt_url": "https://example.invalid/r.png",
            "ocr": {
                "currency_code": "USD",
                "line_items": [{"item_name": name, "item_total_price": 10.0} for name in items],
                "taxes": [{"tax_name": "Sales tax", "tax_amount": 4.12}],
            },
        }
        self.receipts[receipt_id] = receipt
        self.transactions[txn_id]["receipts"].append(receipt_id)
        return receipt

    # -- transport -----------------------------------------------------------

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        self.calls.append((request.method, path, params))
        if self.queued:
            return self.queued.pop(0)
        if path == "/developer/v1/token":
            return self._token(request)
        if request.headers.get("authorization") != f"Bearer {self.valid_token}":
            # recorded on the sandbox: an unknown or expired token is a 404, not a 401
            return _error(404, "DEVELOPER_7002", "Access token with given access_token not found")
        if path == "/developer/v1/transactions":
            return self._list(list(self.transactions.values()), params, self._keep_transaction)
        if path.startswith("/developer/v1/transactions/"):
            txn_id = path.rsplit("/", 1)[-1]
            transaction = self.transactions.get(txn_id)
            if transaction:
                return _json(200, transaction)
            return _error(
                404,
                "DEVELOPER_7002",
                f"The requested TransactionCanonical ({txn_id}) does not exist.",
            )
        if path == "/developer/v1/receipts":
            return self._list(list(self.receipts.values()), params, self._keep_receipt)
        return _json(404, {"error": f"no route {path}"})

    def _token(self, request: httpx.Request) -> httpx.Response:
        expected = base64.b64encode(":".join(self.credentials).encode()).decode()
        if request.headers.get("authorization") != f"Basic {expected}":
            return _json(401, {"error": "invalid_client"})
        assert b"grant_type=client_credentials" in request.content
        self.tokens_issued += 1
        self.valid_token = f"ramp_business_tok_{self.tokens_issued}"
        return _json(
            200,
            {
                "access_token": self.valid_token,
                "expires_in": 864000,
                "scope": "transactions:read receipts:read",
                "token_type": "Bearer",
            },
        )

    def _keep_transaction(self, transaction: dict, params: dict) -> bool:
        if params.get("state") != "ALL" and transaction["state"] == "DECLINED":
            return False
        if "from_date" in params and transaction["user_transaction_time"] < params["from_date"]:
            return False
        holder = transaction["card_holder"]
        if "department_id" in params and holder["department_id"] != params["department_id"]:
            return False
        if "user_id" in params and holder["user_id"] != params["user_id"]:
            return False
        return not ("entity_id" in params and transaction["entity_id"] != params["entity_id"])

    def _keep_receipt(self, receipt: dict, params: dict) -> bool:
        if "transaction_id" in params and receipt["transaction_id"] != params["transaction_id"]:
            return False
        return not ("created_after" in params and receipt["created_at"] <= params["created_after"])

    def _list(self, records, params, keep) -> httpx.Response:
        records = [record for record in records if keep(record, params)]
        records.sort(key=lambda r: (r.get("user_transaction_time") or r.get("created_at"), r["id"]))
        if "start" in params:
            ids = [record["id"] for record in records]
            if params["start"] not in ids:
                # recorded on the sandbox: a start id that no longer exists
                return _error(422, "2006", "Invalid start token for pagination")
            records = records[ids.index(params["start"]) + 1 :]
        size = min(int(params.get("page_size", 20)), self.page_size_cap)
        page, rest = records[:size], records[size:]
        next_url = None
        if rest:
            query = {k: v for k, v in params.items() if k != "start"}
            query["start"] = page[-1]["id"]
            next_url = f"{BASE}/developer/v1/whatever?{urlencode(query)}"
        return _json(200, {"data": page, "page": {"next": next_url}})


def _error(status: int, code: str, message: str) -> httpx.Response:
    """Ramp's error envelope, as returned by the sandbox."""
    return _json(
        status,
        {
            "error_v2": {"message": message, "error_code": code, "error_id": "x", "notes": ""},
            "error": {"message": message, "details": {}},
        },
    )


def _json(status: int, body) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body))


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

NOW = datetime.now(timezone.utc)


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def later(hours: float = 1) -> str:
    return (NOW + timedelta(hours=hours)).isoformat()


def client_for(fake: FakeRamp, sleeps: list | None = None) -> RampClient:
    sleep = sleeps.append if sleeps is not None else (lambda _s: None)
    return RampClient("client", "secret", http=fake.client(), sleep=sleep)


def sync(fake: FakeRamp, state: dict, sleeps: list | None = None, **kwargs):
    """One run: ``(upserts by id, deleted ids, stats)``."""
    stats: dict = {}
    rows = list(_iter_rows(client_for(fake, sleeps), state, stats, **kwargs))
    upserts = {row["id"]: row for row in rows if not row["_deleted"]}
    deletes = {row["id"] for row in rows if row["_deleted"]}
    return upserts, deletes, stats


def serve(fake: FakeRamp, path_suffix: str, status: int, body: bytes = b"{}"):
    """Answer requests whose path ends with ``path_suffix``; returns an undo."""
    original = fake._handle

    def handle(request):
        if request.url.path.endswith(path_suffix):
            return httpx.Response(status, content=body)
        return original(request)

    fake._handle = handle
    return lambda: setattr(fake, "_handle", original)


def receipt_fetches(fake: FakeRamp, after: int) -> list:
    """Per-transaction receipt requests made after call number ``after``."""
    return [call for call in fake.calls[after:] if call[2].get("transaction_id")]
