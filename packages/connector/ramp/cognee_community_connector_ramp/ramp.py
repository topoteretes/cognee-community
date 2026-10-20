"""Ramp connector for cognee: a dlt resource that turns card transactions into memory.

Each transaction becomes one document written around its memo, and is meant to be
handed to ``cognee.remember``::

    await cognee.remember(
        ramp_source(client_id="...", client_secret="...", resource_name="ramp_acme"),
        dataset_name="ramp",
        primary_key="id",
        write_disposition="merge",  # required: the add pipeline defaults to "replace"
        self_improvement=False,  # no enrichment pass on every scheduled sync
    )
"""

import base64
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, NODE_SET_COLUMN

logger = get_logger("ramp_connector")

PRODUCTION_URL = "https://api.ramp.com"
SANDBOX_URL = "https://demo-api.ramp.com"
# read-only scopes; memos come inline on the transaction object
SCOPES = ("transactions:read", "receipts:read")
# the largest page_size the OpenAPI spec allows ("between 2 and 100")
PAGE_SIZE = 100
# covers a monthly expense-report cycle, when memos, receipts and
# settlements arrive after the purchase
DEFAULT_LOOKBACK_DAYS = 60
# the sandbox backfill (1,375 transactions) took 125 requests
DEFAULT_MAX_REQUESTS = 1000
# attempts per request on 5xx or network errors, backing off 1s then 2s
_RETRIES = 3
# 429s waited out (Retry-After) before the run is cut short
_RATE_LIMIT_WAITS = 2
# a longer Retry-After cuts the run short instead of blocking the worker thread
_MAX_RETRY_AFTER = 60.0
_TIMEOUT = 30.0  # seconds
# renew a token this many seconds before it expires
_TOKEN_MARGIN = 60
# runs in a row one transaction may fail before it is given up on; it is then re-read
# once a week, so a record whose access returns (or that was deleted) is caught later
_MAX_ATTEMPTS = 5
_RETRY_GIVEN_UP_DAYS = 7
# a receipt with no OCR data yet is fetched again for this long after its upload; older
# ones are taken as unreadable (the sandbox's receipts never get OCR)
_OCR_WAIT_DAYS = 3
# transaction states that mean the money never moved
_DEAD_STATES = {"DECLINED", "ERROR"}
# fields that change without changing the document; ignored when deciding to re-render
_VOLATILE_FIELDS = ("updated_at", "synced_at", "sync_status")
_FRACTION = re.compile(r"\.(\d+)")
# the record id in /developer/v1/<collection>/<id>
_ID_SEGMENT = re.compile(r"^(/developer/v1/[a-z_-]+)/[^/]+")
_TRANSACTIONS = "/developer/v1/transactions"
_RECEIPTS = "/developer/v1/receipts"
_TOKEN = "/developer/v1/token"


class RampSourceError(RuntimeError):
    """Base class of the errors this source raises."""


class RampAPIError(RampSourceError):
    """Ramp answered with an error, or could not be reached."""


class RampUnavailableError(RampAPIError):
    """Ramp could not be reached, or kept answering 5xx: not about any one record."""


class RampAuthError(RampSourceError):
    """Ramp rejected the client credentials or the token; no request can succeed."""


class RampAccessError(RampAPIError):
    """Ramp refused one request (HTTP 403), e.g. a record the app may not read."""


class RampStaleTokenError(RampAPIError):
    """Ramp refused a saved pagination token (HTTP 422 "Invalid start token")."""


class RampNotFoundError(RampAPIError):
    """Ramp has no such record (HTTP 404)."""


class RampRateLimitedError(RampSourceError):
    """Ramp kept answering HTTP 429."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class RampClient:
    """Ramp Developer API client: client-credentials token, retries, rate limits."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = PRODUCTION_URL,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not client_id or not client_secret:
            raise ValueError("Ramp needs a client_id and a client_secret")
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        # ties sync state to one Ramp app, so another business's state is never reused
        self.identity = _app_identity(client_id)
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=_TIMEOUT)
        self._sleep = sleep
        self._token: str | None = None
        self._token_expires = 0.0

    def __repr__(self) -> str:
        return f"RampClient(base_url={self.base_url!r})"

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` and return its JSON body, renewing the token once if it is refused."""
        response = self._send("GET", path, params=params, token=self._access_token())
        if _token_refused(response):
            self._token = None  # revoked early: renew once
            response = self._send("GET", path, params=params, token=self._access_token())
        return self._parse(response, path)

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires - _TOKEN_MARGIN:
            return self._token
        response = self._send(
            "POST",
            _TOKEN,
            data={"grant_type": "client_credentials", "scope": " ".join(SCOPES)},
            basic=True,
        )
        try:
            body = self._parse(response, _TOKEN)
        except RampAPIError as exc:
            # no token means no request can succeed: never a per-record failure
            raise RampAuthError(f"Ramp issued no access token: {exc}") from exc
        if not body.get("access_token"):
            raise RampAuthError("Ramp issued no access token; check the app's client credentials")
        self._token = body["access_token"]
        self._token_expires = time.time() + float(body.get("expires_in") or 0)
        return self._token

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
        token: str | None = None,
        basic: bool = False,
    ) -> httpx.Response:
        headers = {"Accept": "application/json"}
        headers["Authorization"] = f"Basic {self._basic}" if basic else f"Bearer {token}"
        failures = waits = 0
        while True:
            try:
                response = self._http.request(
                    method, self.base_url + path, params=params, data=data, headers=headers
                )
            except httpx.TransportError as exc:
                failures += 1
                if failures == _RETRIES:
                    raise RampUnavailableError(
                        f"Ramp could not be reached for {_endpoint(path)}"
                    ) from exc
                self._sleep(2 ** (failures - 1))
                continue
            if response.status_code == 429:
                waits += 1
                delay = _retry_after(response)
                if waits > _RATE_LIMIT_WAITS or delay > _MAX_RETRY_AFTER:
                    raise RampRateLimitedError(f"Ramp kept rate limiting {_endpoint(path)}")
                self._sleep(delay)
                continue
            if response.status_code >= 500:
                failures += 1
                if failures < _RETRIES:
                    self._sleep(2 ** (failures - 1))
                    continue
            return response

    @staticmethod
    def _parse(response: httpx.Response, path: str) -> Any:
        status = response.status_code
        endpoint = _endpoint(path)
        if status == 401 or _token_refused(response):
            raise RampAuthError(f"Ramp refused {endpoint} (HTTP {status}); check the credentials")
        if status == 403:
            # fatal on a listing (nothing is readable), retried per record elsewhere
            raise RampAccessError(f"Ramp refused {endpoint} (HTTP 403); check the app's scopes")
        if status == 404 and _record_missing(response):
            raise RampNotFoundError(f"Ramp has no such record at {endpoint}")
        if status == 422 and "start token" in _error_message(response).lower():
            raise RampStaleTokenError(f"Ramp refused a saved page token for {endpoint}")
        if status >= 500:
            raise RampUnavailableError(f"Ramp answered HTTP {status} for {endpoint}")
        if status >= 400:
            raise RampAPIError(f"Ramp answered HTTP {status} for {endpoint}")
        try:
            body = response.json()
        except ValueError as exc:
            raise RampAPIError(f"Ramp sent a non-JSON body for {endpoint}") from exc
        if not isinstance(body, dict):
            raise RampAPIError(f"Ramp sent an unexpected body for {endpoint}")
        return body


def _endpoint(path: str) -> str:
    """``path`` with record ids replaced, for messages and logs."""
    return _ID_SEGMENT.sub(r"\1/{id}", path)


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error_v2") or body.get("error") if isinstance(body, dict) else None
    return str((error or {}).get("message") or "") if isinstance(error, dict) else ""


def _token_refused(response: httpx.Response) -> bool:
    """Ramp answers an unknown or expired token with HTTP 404, not 401.

    Seen on the sandbox: ``404 DEVELOPER_7002 "Access token with given access_token not
    found"``, the same status and code as a missing transaction. Reading it as "record
    gone" would forget live transactions whenever a token expired mid-run.
    """
    if response.status_code == 401:
        return True
    return response.status_code == 404 and "access token" in _error_message(response).lower()


def _record_missing(response: httpx.Response) -> bool:
    """A 404 naming the requested record ("The requested X (<id>) does not exist.").

    Any other 404 is an error that deletes nothing.
    """
    message = _error_message(response)
    return message.startswith("The requested ") and message.endswith("does not exist.")


def _next_start(next_url: str | None) -> str | None:
    """The ``start`` token of ``page.next``; the URL itself is never followed."""
    if not next_url:
        return None
    values = parse_qs(urlparse(next_url).query).get("start")
    return values[0] if values else None


def _app_identity(client_id: str) -> str:
    return hashlib.sha256(client_id.encode()).hexdigest()[:16]


def _row_id(transaction_id: str) -> str:
    return f"transaction:{transaction_id}"


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return 5.0


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_transaction(
    transaction: dict, receipts: list[dict], require_context: bool = True
) -> dict | None:
    """Turn a transaction and its receipts into a document row.

    With ``require_context``, returns None when the transaction carries no context
    (memo, receipt items, attendees, trip, policy or dispute notes): an amount alone
    is not knowledge.
    """
    memo = _text(transaction.get("memo"))
    holder = transaction.get("card_holder") or {}
    person = " ".join(filter(None, [holder.get("first_name"), holder.get("last_name")]))
    department = _text(holder.get("department_name"))
    merchant = _text(transaction.get("merchant_name")) or "an unknown merchant"
    date = _text(transaction.get("user_transaction_time"))[:10] or "an unknown date"

    context: list[str] = []
    for item in transaction.get("line_items") or []:
        if note := _text(item.get("memo")):
            amount = _line_item_amount(item.get("amount"))
            context.append(f"Line item: {note}" + (f" ({amount})" if amount else ""))
    if trip := _text(transaction.get("trip_name")):
        context.append(f"Trip: {trip}")
    # sorted, so a different order from the API doesn't re-cognify the document
    attendees = sorted(
        _text(a.get("name")) for a in transaction.get("attendees") or [] if a.get("name")
    )
    if attendees:
        context.append(f"Attendees: {', '.join(attendees)}")
    for receipt in receipts:
        context.extend(_receipt_lines(receipt))
    for violation in transaction.get("policy_violations") or []:
        if note := _text(violation.get("memo")):
            context.append(f"Policy note: {note}")
    for dispute in transaction.get("disputes") or []:
        if note := _text(dispute.get("memo")):
            context.append(f"Dispute ({_text(dispute.get('type'))}): {note}")
    if require_context and not memo and not context:
        return None

    who = person or "Someone"
    if person and department:
        who += f" ({department})"
    where = merchant + _location(transaction.get("merchant_location"))
    facts = [
        _text(transaction.get("sk_category_name")),
        _transaction_amount(transaction),
        _text(transaction.get("state")).lower(),
    ]
    lines = []
    if memo:
        lines.append(f'Memo: "{memo}"')
    lines.append(f"{who} paid {where} on {date}: {', '.join(f for f in facts if f)}.")
    if transaction.get("original_transaction_id"):
        lines.append("Refund of an earlier transaction.")
    lines.extend(context)
    coding = sorted(
        f"{_text((s.get('category_info') or {}).get('name')) or 'Field'}: "
        f"{_text(s.get('display_name') or s.get('name'))}"
        for s in transaction.get("accounting_field_selections") or []
        if s.get("display_name") or s.get("name")
    )
    if coding:
        lines.append("Accounting: " + "; ".join(coding))

    title = f'Memo: "{memo}"' if memo else f"{merchant} purchase on {date}"
    return {
        "id": _row_id(transaction["id"]),
        "title": title,
        "content": "\n".join(lines),
        NODE_SET_COLUMN: [f"department:{department}"] if department else None,
        "_deleted": False,
    }


def _receipt_lines(receipt: dict) -> list[str]:
    ocr = receipt.get("ocr") or {}
    names = [n for item in ocr.get("line_items") or [] if (n := _text(item.get("item_name")))]
    lines = []
    if names:
        shown = ", ".join(names[:10]) + (", ..." if len(names) > 10 else "")
        lines.append(f"Receipt: {len(names)} line item{'s' if len(names) != 1 else ''} ({shown})")
    for tax in ocr.get("taxes") or []:
        if tax.get("tax_amount") is not None:
            name = _text(tax.get("tax_name")) or "tax"
            lines.append(
                f"Receipt {name}: {tax['tax_amount']} {_text(ocr.get('currency_code'))}".strip()
            )
    return lines


def _transaction_amount(transaction: dict) -> str:
    amount = transaction.get("entity_amount") or {}
    if amount.get("value") is None:
        return ""
    rate = transaction.get("minor_unit_conversion_rate") or 100
    return f"{amount['value'] / rate:,.2f} {_text(amount.get('currency'))}".strip()


def _line_item_amount(amount: dict | None) -> str:
    if not amount or amount.get("amount") is None:
        return ""
    rate = amount.get("minor_unit_conversion_rate") or 100
    return f"{amount['amount'] / rate:,.2f} {_text(amount.get('currency_code'))}".strip()


def _location(location: dict | None) -> str:
    parts = [_text((location or {}).get(key)) for key in ("city", "state", "country")]
    parts = [part for part in parts if part]
    return f" ({', '.join(parts)})" if parts else ""


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _hash(value: Any) -> str:
    # 64 bits is plenty to notice a change, and keeps per-transaction dlt state small
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode())
    return digest.hexdigest()[:16]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normal_time(value: str | None) -> str | None:
    """An ISO 8601 time as UTC ``YYYY-MM-DDTHH:MM:SSZ``, so times compare as text."""
    moment = _parse_time(value)
    return _iso(moment) if moment else None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    # Python 3.10's fromisoformat only takes 3 or 6 fractional digits
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], value.replace("Z", "+00:00"))
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class _CutShortError(Exception):
    """Internal: stop the run here, keeping the state reached so far."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Walker:
    """One run, writing its progress into ``state`` (dlt resource state, or a dict in tests)."""

    def __init__(
        self,
        client: Any,
        state: dict,
        stats: dict[str, int],
        *,
        since: str | None,
        lookback: timedelta,
        include_declined: bool,
        require_context: bool,
        department_ids: set[str],
        user_ids: set[str],
        entity_id: str | None,
        max_requests: int,
        check_active: Callable[[], None] | None,
        adopt_state_from: str | None = None,
    ):
        self.client = client
        self.adopt_from = _app_identity(adopt_state_from) if adopt_state_from else None
        self.state = state
        self.stats = stats
        for key in ("scanned", "skipped", "deleted", "failed"):
            stats.setdefault(key, 0)
        self.since = _normal_time(since)
        self.lookback = lookback
        self.include_declined = include_declined
        self.require_context = require_context
        self.department_ids = department_ids
        self.user_ids = user_ids
        self.entity_id = entity_id
        self.max_requests = max_requests
        self.check_active = check_active
        self.requests = 0
        # a transaction dated after this (Ramp has future-dated ones) never moves the cursor
        self.run_start = _iso(datetime.now(timezone.utc))
        self.scope = _hash(
            [
                sorted(department_ids),
                sorted(user_ids),
                entity_id,
                include_declined,
                require_context,
                self.since,
            ]
        )
        self.rows: dict[str, list] = state.setdefault("rows", {})
        # input keys of listed transactions that produced no document, for this window
        self.quiet: dict[str, str] = state.setdefault("quiet", {})
        self.recheck: list[str] = state.setdefault("recheck", [])
        self.queued: set[str] = set(self.recheck)
        self.failed: list[str] = state.setdefault("recheck_failed", [])
        # {transaction id: [failed reads in a row, time of the last one]}
        self.attempts: dict[str, list] = state.setdefault("attempts", {})
        # transactions this run's listing already rendered with their current receipts
        self.walked: set[str] = set()

    # -- requests ----------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        if self.check_active is not None:
            self.check_active()
        if self.requests >= self.max_requests:
            raise _CutShortError("budget")
        self.requests += 1
        try:
            return self.client.get(path, params)
        except RampRateLimitedError:
            raise _CutShortError("rate_limit") from None

    def _pages(self, path: str, params: dict[str, Any], progress: dict | None = None):
        """Yield each page's records, following ``page.next`` via its ``start`` token.

        With ``progress``, the token of the next unread page is kept there, so a
        cut-short walk resumes at the page it was reading.
        """
        start = (progress or {}).get("start")
        while True:
            query = {**params, "page_size": PAGE_SIZE}
            if start:
                query["start"] = start
            try:
                body = self._get(path, query)
            except RampStaleTokenError:
                if not (progress and progress.get("start")):
                    raise
                # the token is the id of the last record read, and Ramp answers 422 once
                # that record is gone (an expired authorization, seen on the sandbox):
                # restart the listing; records already read are skipped by their hashes
                logger.warning("Ramp refused a saved page token; restarting the listing.")
                start = progress["start"] = None
                progress["restarted"] = True
                continue
            data, page = body.get("data"), body.get("page")
            # a malformed listing must fail, never read as "no transactions" (which would
            # queue every stored row for a deletion check)
            if not isinstance(data, list) or not isinstance(page, dict):
                raise RampAPIError(
                    f"Ramp sent a listing without data or page for {_endpoint(path)}"
                )
            yield data
            start = _next_start(page.get("next"))
            if progress is not None:
                progress["start"] = start
            if not start:
                return

    # -- scope -------------------------------------------------------------
    def _in_scope(self, transaction: dict) -> bool:
        holder = transaction.get("card_holder") or {}
        if self.department_ids and holder.get("department_id") not in self.department_ids:
            return False
        if self.user_ids and holder.get("user_id") not in self.user_ids:
            return False
        if self.entity_id and transaction.get("entity_id") != self.entity_id:
            return False
        moment = _normal_time(transaction.get("user_transaction_time"))
        if self.since and (moment or "") < self.since:
            return False
        return self.include_declined or transaction.get("state") not in _DEAD_STATES

    def _listing_filters(self) -> dict[str, Any]:
        params: dict[str, Any] = {"order_by_date_asc": "true"}
        if self.include_declined:
            params["state"] = "ALL"
        # Ramp filters one id at a time; several are filtered here instead
        if len(self.department_ids) == 1:
            params["department_id"] = next(iter(self.department_ids))
        if len(self.user_ids) == 1:
            params["user_id"] = next(iter(self.user_ids))
        if self.entity_id:
            params["entity_id"] = self.entity_id
        return params

    # -- rows --------------------------------------------------------------
    def _process(self, transaction: dict) -> dict | None:
        """The row to emit for a listed or re-read transaction, or None when nothing changed.

        The input key hashes the selection and every non-volatile field (including the
        receipt id list), so an unchanged transaction is skipped before its receipts are read.
        """
        row_id = _row_id(transaction["id"])
        self.stats["scanned"] += 1
        stable = {k: v for k, v in transaction.items() if k not in _VOLATILE_FIELDS}
        input_key = _hash([self.scope, stable])
        stored = self.rows.get(row_id)
        if (stored and stored[2] == input_key) or self.quiet.get(row_id) == input_key:
            self.stats["skipped"] += 1
            return None

        row = None
        receipts: list[dict] = []
        if self._in_scope(transaction):
            receipts = self._receipts_for(transaction["id"]) if transaction.get("receipts") else []
            row = render_transaction(transaction, receipts, self.require_context)
        if row is None:
            # remembered (until it leaves the window) so its receipts aren't fetched again
            if not self._ocr_pending(receipts):
                self.quiet[row_id] = input_key
            return self._tombstone(row_id) if stored else None
        self.quiet.pop(row_id, None)
        fingerprint = _hash(row)
        moment = _normal_time(transaction.get("user_transaction_time"))
        # a recent receipt Ramp hasn't read yet (``ocr`` null) gets its line items later
        # without changing the transaction, so it is fetched again until OCR lands
        self.rows[row_id] = [fingerprint, moment, "" if self._ocr_pending(receipts) else input_key]
        if stored and stored[0] == fingerprint:
            self.stats["skipped"] += 1
            return None
        return row

    def _ocr_pending(self, receipts: list[dict]) -> bool:
        horizon = _iso(_parse_time(self.run_start) - timedelta(days=_OCR_WAIT_DAYS))
        return any(
            receipt.get("ocr") is None
            and (_normal_time(receipt.get("created_at")) or "") >= horizon
            for receipt in receipts
        )

    def _tombstone(self, row_id: str) -> dict:
        self.rows.pop(row_id, None)
        self.attempts.pop(row_id.removeprefix("transaction:"), None)
        self.stats["deleted"] += 1
        return {"id": row_id, "_deleted": True}

    def _receipts_for(self, transaction_id: str) -> list[dict]:
        receipts: list[dict] = []
        params = {"transaction_id": transaction_id, "include_ocr_data": "true"}
        for page in self._pages(_RECEIPTS, params):
            receipts.extend(page)
        return receipts

    # -- the listing -------------------------------------------------------
    def _new_walk(self) -> dict:
        cursor = _parse_time(self.state.get("cursor"))
        start = self.since
        if cursor is not None:
            start = _iso(cursor - self.lookback)
            if self.since:
                start = max(start, self.since)
        return {"from": start, "full": cursor is None, "listing": {}, "seen": [], "latest": None}

    def _walk(self) -> Iterator[dict]:
        """List the window oldest first; resumable through ``state["walk"]``."""
        walk = self.state["walk"] = self.state.get("walk") or self._new_walk()
        params = self._listing_filters()
        if walk["from"]:
            params["from_date"] = walk["from"]
        seen = set(walk["seen"])
        for page in self._pages(_TRANSACTIONS, params, walk["listing"]):
            if walk["listing"].pop("restarted", False):
                # the listing starts over, so what it saw before (including the record
                # that vanished) must be seen again to count as present
                walk["seen"].clear()
                seen.clear()
            for transaction in page:
                row_id = _row_id(transaction["id"])
                if row_id not in seen:  # a restarted listing returns records again
                    seen.add(row_id)
                    walk["seen"].append(row_id)
                self.walked.add(transaction["id"])
                if moment := _normal_time(transaction.get("user_transaction_time")):
                    walk["latest"] = max(walk["latest"] or "", min(moment, self.run_start))
                row = self._guarded(transaction["id"], self._process, transaction)
                if row is not None:
                    yield row
        self._close_walk(walk)

    def _close_walk(self, walk: dict) -> None:
        """A complete listing: re-check stored rows in its range that it did not return.

        A full listing covers everything from ``since``, so every unseen row is checked;
        a window listing only covers rows dated from its start.
        """
        seen = set(walk["seen"])
        window_from = walk["from"] or ""
        for row_id in set(self.quiet) - seen:
            del self.quiet[row_id]
        for row_id, (_, moment, _) in self.rows.items():
            if row_id not in seen and (walk["full"] or (moment or "") >= window_from):
                self._queue(row_id.removeprefix("transaction:"))
        if walk["latest"]:
            self.state["cursor"] = walk["latest"]
        self.state["walk"] = None

    # -- late receipts on older transactions -------------------------------
    def _late_receipts(self) -> None:
        """Queue transactions that got a receipt since the last run for a re-read."""
        cursor = self.state["receipt_cursor"]
        # resumable like the transaction listing, so a backlog bigger than one run's
        # budget is read over several runs instead of from the same cursor every time
        listing = self.state["receipt_walk"] = self.state.get("receipt_walk") or {
            "start": None,
            "newest": cursor,
        }
        for page in self._pages(_RECEIPTS, {"created_after": cursor}, listing):
            for receipt in page:
                transaction_id = receipt.get("transaction_id")
                if transaction_id and transaction_id not in self.walked:
                    self._queue(transaction_id)
                created = _normal_time(receipt.get("created_at"))
                if created:
                    listing["newest"] = max(listing["newest"], min(created, self.run_start))
        # safe to move on: the transactions are queued in state, and a failed re-read is
        # retried from ``recheck_failed``
        self.state["receipt_cursor"] = listing["newest"]
        self.state["receipt_walk"] = None

    # -- re-reads by id: deletion checks and late receipts -----------------
    def _queue(self, transaction_id: str) -> None:
        if transaction_id not in self.queued:
            self.queued.add(transaction_id)
            self.recheck.append(transaction_id)

    def _refresh(self, transaction_id: str) -> dict | None:
        try:
            transaction = self._get(f"{_TRANSACTIONS}/{transaction_id}")
        except RampNotFoundError:
            row_id = _row_id(transaction_id)
            return self._tombstone(row_id) if row_id in self.rows else None
        return self._process(transaction)

    def _recheck(self) -> Iterator[dict]:
        while self.recheck:
            transaction_id = self.recheck[0]
            # skip ids that already failed this run, or were given up on; a cut-short
            # read raises before the pop, so the id stays queued
            skip = transaction_id in self.failed or self._given_up(transaction_id)
            row = None if skip else self._guarded(transaction_id, self._refresh, transaction_id)
            self.recheck.pop(0)
            self.queued.discard(transaction_id)
            if row is not None:
                yield row

    def _guarded(self, transaction_id: str, step: Callable[..., Any], *args: Any) -> Any:
        """Run one record's step; an API error on it is retried next run, not fatal.

        A refused token (RampAuthError) still aborts the run: it is not about one record.
        """
        try:
            result = step(*args)
        except RampAPIError as exc:
            # an outage (5xx, network) says nothing about the record, so only errors
            # about this record (403, 422, an unrecognised 404) count toward giving up
            if not isinstance(exc, RampUnavailableError):
                attempts = self.attempts.get(transaction_id, [0, None])[0] + 1
                self.attempts[transaction_id] = [attempts, self.run_start]
                if attempts >= _MAX_ATTEMPTS:
                    # an error that never clears: stop re-reading it by id (a listing that
                    # returns it still tries); a failed read never deletes
                    if attempts == _MAX_ATTEMPTS:
                        self.stats["abandoned"] = self.stats.get("abandoned", 0) + 1
                        logger.warning("Ramp kept failing on one transaction; giving up: %s", exc)
                    else:
                        logger.debug("Ramp still failing on a given-up transaction: %s", exc)
                    return None
            self.stats["failed"] += 1
            if transaction_id not in self.failed:
                self.failed.append(transaction_id)
            logger.warning("Ramp failed on one transaction, retried next run: %s", exc)
            return None
        self.attempts.pop(transaction_id, None)
        return result

    def _given_up(self, transaction_id: str) -> bool:
        """Given up on, and re-read less than a week ago."""
        count, last = self.attempts.get(transaction_id, [0, None])
        if count < _MAX_ATTEMPTS:
            return False
        retry_from = _parse_time(self.run_start) - timedelta(days=_RETRY_GIVEN_UP_DAYS)
        return (last or "") > _iso(retry_from)

    # -- the run -----------------------------------------------------------
    def _check_state(self) -> None:
        identity = getattr(self.client, "identity", None)
        known = self.state.get("identity")
        if identity and known and known != identity:
            # only the named previous app's state is taken over, so leaving the option set
            # can never adopt another business's state later
            if known != self.adopt_from:
                raise RampSourceError(
                    "This resource_name holds another Ramp app's sync state. Use one "
                    "resource_name per Ramp business; reusing it would forget that "
                    "business's transactions. If this is a new app for the same business, "
                    "pass adopt_state_from=<the previous app's client_id>."
                )
            # same business, new app: keep the stored rows, list everything again
            self.state.update(cursor=None, walk=None)
        if identity:
            self.state["identity"] = identity
        if self.state.get("scope") not in (None, self.scope):
            # the selection changed: list everything again, and give every record a
            # fresh set of attempts
            self.state.update(cursor=None, walk=None)
            self.attempts.clear()
        self.state["scope"] = self.scope

    def run(self) -> Iterator[dict]:
        self._check_state()
        first_run = "receipt_cursor" not in self.state
        if first_run:
            # the backfill reads every receipt it renders, so this cursor starts before it
            self.state["receipt_cursor"] = self.run_start
        # last run's failures go to the back of the queue, so they can't block it
        for transaction_id in self.failed:
            self._queue(transaction_id)
        self.failed.clear()
        yield from self._recheck()
        yield from self._walk()
        if not first_run:
            self._late_receipts()
        yield from self._recheck()


def _iter_rows(
    client: Any,
    state: dict,
    stats: dict[str, int],
    *,
    since: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    include_declined: bool = False,
    require_context: bool = True,
    department_ids: list[str] | None = None,
    user_ids: list[str] | None = None,
    entity_id: str | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    check_active: Callable[[], None] | None = None,
    adopt_state_from: str | None = None,
) -> Iterator[dict]:
    """Yield the changed rows of a Ramp business.

    Independent of dlt, so tests can drive it with a plain dict.
    """
    walker = _Walker(
        client,
        state,
        stats,
        since=since,
        lookback=timedelta(days=lookback_days),
        include_declined=include_declined,
        require_context=require_context,
        department_ids=set(department_ids or []),
        user_ids=set(user_ids or []),
        entity_id=entity_id,
        max_requests=max_requests,
        check_active=check_active,
        adopt_state_from=adopt_state_from,
    )
    try:
        yield from walker.run()
    except _CutShortError as cut:
        stats["failed"] = max(1, stats.get("failed", 0))
        stats[f"failed_{cut.reason}"] = 1
        logger.warning("Ramp sync stopped early (%s); it resumes on the next run.", cut.reason)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------
def ramp_source(
    *,
    resource_name: str,
    client_id: str | None = None,
    client_secret: str | None = None,
    sandbox: bool = False,
    service: Any = None,
    check_active: Callable[[], None] | None = None,
    since: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    include_declined: bool = False,
    require_context: bool = True,
    department_ids: list[str] | None = None,
    user_ids: list[str] | None = None,
    entity_id: str | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    adopt_state_from: str | None = None,
):
    """Return a ``dlt`` resource that yields a Ramp business's transactions for ``remember``.

    Args:
        resource_name: Stable name of this connection, one per Ramp business. It scopes
            the sync state and the cleanup of forgotten transactions, so keep it fixed.
        client_id, client_secret: A Ramp developer app with client credentials enabled
            and the ``transactions:read`` and ``receipts:read`` scopes.
        sandbox: Use Ramp's sandbox (``demo-api.ramp.com``) instead of production.
        service: Pre-built client, mainly an injection point for tests. Without an
            ``identity`` attribute (``RampClient`` has one), the app check is skipped.
        check_active: Optional host authorization checkpoint, called before each request.
        since: Only sync transactions from this ISO 8601 time on (the first backfill).
        lookback_days: How far behind the cursor each run re-reads, to catch memos,
            receipts and settlements added after the purchase.
        include_declined: Also ingest declined (and errored) transactions.
        require_context: Skip transactions with no memo, receipt items, attendees, trip,
            policy or dispute notes.
        department_ids, user_ids, entity_id: Only sync these departments, card holders
            or business entity.
        max_requests: Request budget of one run. A big backfill finishes over several runs.
        adopt_state_from: ``client_id`` of a previous Ramp app of the same business whose
            sync state this run may take over; everything is listed again.

    Returns:
        A ``dlt`` resource configured with ``primary_key="id"``,
        ``write_disposition="merge"`` and a ``_deleted`` hard-delete column.
    """
    import dlt

    if getattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 0) < 2:
        raise RuntimeError("Ramp sync requires cognee>=1.6.4 (per-row node sets).")
    if not isinstance(resource_name, str) or not resource_name.strip():
        raise ValueError("ramp_source needs a resource_name, one per Ramp business")
    if lookback_days < 0:
        raise ValueError("lookback_days must be zero or more")
    if max_requests < 3:
        # a listing page, a receipts fetch and a re-read must fit, or a run can stall
        raise ValueError("max_requests must be at least 3")
    if since is not None and _parse_time(since) is None:
        raise ValueError("since must be an ISO 8601 time, e.g. 2026-01-01T00:00:00Z")
    # built here so missing credentials fail at construction and the closure holds no raw secret
    client = service
    if client is None:
        client = RampClient(
            client_id or "",
            client_secret or "",
            base_url=SANDBOX_URL if sandbox else PRODUCTION_URL,
        )
    stats: dict[str, int] = {}

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={
            "_deleted": {"data_type": "bool", "hard_delete": True},
            **dlt_utils.NODE_SET_COLUMN_HINT,
        },
    )
    def ramp_transactions():
        stats.clear()
        stats.update(scanned=0, skipped=0, deleted=0, failed=0)
        rows = _iter_rows(
            client,
            dlt.current.resource_state(),
            stats,
            since=since,
            lookback_days=lookback_days,
            include_declined=include_declined,
            require_context=require_context,
            department_ids=department_ids,
            user_ids=user_ids,
            entity_id=entity_id,
            max_requests=max_requests,
            check_active=check_active,
            adopt_state_from=adopt_state_from,
        )
        yield from dlt_utils.guarded_rows(rows, check_active)

    resource = ramp_transactions()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "ramp")
    setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
