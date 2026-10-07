"""DLT source for Ramp transactions, memos, and receipts (full-snapshot sync + forget-on-delete).

Fetches Ramp corporate card transactions, employee memos, and OCR receipt data,
then formats them as markdown documents for cognee's ingestion pipeline.

Unlike the relational dlt path (SQL/CSV), Ramp transactions are ingested as
*normal documents*: the source declares ``cognee_document_source = "ramp"``,
so ``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "ramp"``
(not ``"dlt"``). ``is_dlt_sourced`` therefore returns False and each transaction flows
through the standard cognify entity-extraction pipeline — the right treatment
for expense memos, merchant semantics, line items, and audit context.

The source defaults to a full snapshot: ``write_disposition="replace"`` rewrites
staging with transactions currently cleared/active in the sync window. Deletions,
declines, or purged items drop out of the snapshot, allowing cognee's ``orphan_cleanup``
to remove dead nodes from the knowledge graph and vector stores.

Watch out:
Memos and receipts are often added days after the transaction takes place.
A naive filter on ``user_transaction_time`` alone would miss delayed memos.
This connector provides a lookback window (default 30 days) and persistent
watermark state to catch edits and newly uploaded receipts.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timedelta
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("ramp_connector")

RAMP_TABLE_NAME = "ramp_transactions"
RAMP_SOURCE_NAME = "ramp"

_BASE_URL = "https://api.ramp.com"
_TOKEN_URL = "https://api.ramp.com/developer/v1/token"
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0


def _get_retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Calculate exponential retry delay honoring Retry-After headers."""
    if response is not None and "retry-after" in response.headers:
        try:
            return float(response.headers["retry-after"])
        except (ValueError, TypeError):
            pass
    return _BASE_BACKOFF * (2**attempt)


class RampClient:
    """Synchronous HTTP client for Ramp Developer API v1."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        access_token: str | None = None,
        base_url: str = _BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id or os.getenv("RAMP_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("RAMP_CLIENT_SECRET")
        self.access_token = access_token or os.getenv("RAMP_ACCESS_TOKEN")
        self.transport = transport

        if not self.access_token and not (self.client_id and self.client_secret):
            raise ValueError(
                "Ramp authentication required. Provide either access_token or "
                "(client_id, client_secret) via arguments or environment variables."
            )

        self._client = httpx.Client(
            timeout=30.0,
            transport=self.transport,
            headers={"User-Agent": "cognee-community-connector-ramp/0.1.0"},
        )

        if not self.access_token and self.client_id and self.client_secret:
            self._fetch_access_token()

    def _fetch_access_token(self) -> None:
        """Fetch OAuth 2.0 access token using client credentials."""
        token_url = f"{self.base_url}/developer/v1/token"
        data = {
            "grant_type": "client_credentials",
            "scope": "transactions:read receipts:read",
        }
        resp = self._client.post(
            token_url,
            data=data,
            auth=(self.client_id, self.client_secret),
        )
        resp.raise_for_status()
        payload = resp.json()
        self.access_token = payload["access_token"]

    def _get_auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}" if path.startswith("/") else path
        for attempt in range(_MAX_RETRIES):
            try:
                headers = self._get_auth_headers()
                resp = self._client.request(method, url, headers=headers, params=params)
                if resp.status_code == 401 and self.client_id and self.client_secret:
                    logger.info("Ramp token expired (401), refreshing token...")
                    self._fetch_access_token()
                    continue
                if resp.status_code in (429, 500, 502, 503, 504):
                    delay = _get_retry_delay(resp, attempt)
                    logger.warning(
                        "Ramp API returned %s, retrying in %.2fs",
                        resp.status_code,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.NetworkError) as err:
                if attempt == _MAX_RETRIES - 1:
                    raise
                delay = _get_retry_delay(None, attempt)
                logger.warning("Network error calling %s (%s), retrying in %.2fs", url, err, delay)
                time.sleep(delay)

        raise RuntimeError(f"Exceeded max retries calling Ramp API: {url}")

    def list_transactions(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        entity_id: str | None = None,
        department_id: str | None = None,
        user_id: str | None = None,
        page_size: int = 100,
        start: str | None = None,
    ) -> dict[str, Any]:
        """Fetch transactions page from /developer/v1/transactions."""
        params: dict[str, Any] = {"page_size": page_size}
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date
        if entity_id:
            params["entity_id"] = entity_id
        if department_id:
            params["department_id"] = department_id
        if user_id:
            params["user_id"] = user_id
        if start:
            params["start"] = start

        return self._request("GET", "/developer/v1/transactions", params=params)

    def list_receipts(
        self,
        transaction_id: str | None = None,
        include_ocr_data: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch receipts for a transaction from /developer/v1/receipts."""
        params: dict[str, Any] = {}
        if transaction_id:
            params["transaction_id"] = transaction_id
        if include_ocr_data:
            params["include_ocr_data"] = "true"

        data = self._request("GET", "/developer/v1/receipts", params=params)
        return data.get("data", [])

    def close(self) -> None:
        """Close underlying HTTP client."""
        self._client.close()


def _transaction_to_document(
    txn: dict[str, Any],
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Convert Ramp transaction and its receipts into a structured markdown document."""
    txn_id = txn.get("id", "unknown")
    merchant = txn.get("merchant_name") or "Unknown Merchant"
    amount = txn.get("amount", 0)
    currency = txn.get("currency_code", "USD")
    state = txn.get("state", "CLEARED")
    date_str = txn.get("user_transaction_time") or txn.get("created_at") or "N/A"
    cardholder = txn.get("cardholder_name") or "Unknown Cardholder"
    category = txn.get("merchant_category_code") or txn.get("category_name") or "Uncategorized"
    memo = (txn.get("memo") or "").strip()

    doc_lines = [
        f"# Ramp Transaction: {merchant} ({amount} {currency})",
        "",
        f"- **Transaction ID**: {txn_id}",
        f"- **Date**: {date_str}",
        f"- **Cardholder**: {cardholder}",
        f"- **Merchant**: {merchant}",
        f"- **Amount**: {amount} {currency}",
        f"- **Category**: {category}",
        f"- **Status / State**: {state}",
    ]

    if memo:
        doc_lines.extend(["", "## Expense Memo & Business Purpose", "", memo])

    if receipts:
        doc_lines.extend(["", f"## Receipts ({len(receipts)})"])
        for idx, rec in enumerate(receipts, start=1):
            r_id = rec.get("id", f"receipt-{idx}")
            doc_lines.extend(["", f"### Receipt {idx} ({r_id})"])
            ocr = rec.get("ocr_data") or {}
            if isinstance(ocr, dict):
                vendor = ocr.get("vendor_name")
                total = ocr.get("total_amount")
                if vendor:
                    doc_lines.append(f"- **OCR Vendor**: {vendor}")
                if total:
                    doc_lines.append(f"- **OCR Total**: {total}")
                items = ocr.get("line_items") or []
                if items:
                    doc_lines.append("- **Line Items**:")
                    for itm in items:
                        desc = itm.get("description", "Item")
                        cost = itm.get("amount", "")
                        doc_lines.append(f"  - {desc} ({cost})")
            elif isinstance(ocr, str) and ocr.strip():
                doc_lines.append(f"- **OCR Text**: {ocr.strip()}")

    full_text = "\n".join(doc_lines)
    content_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()

    return {
        "id": f"ramp:txn:{txn_id}",
        "data_id": f"ramp:{content_hash}",
        "name": f"{merchant} - {amount} {currency}",
        "text": full_text,
        "amount": amount,
        "currency": currency,
        "merchant": merchant,
        "state": state,
        "date": date_str,
        "external_metadata": {
            "source": RAMP_SOURCE_NAME,
            "transaction_id": txn_id,
            "cardholder": cardholder,
            "has_memo": bool(memo),
            "receipt_count": len(receipts),
        },
    }


def ramp_source(
    client_id: str | None = None,
    client_secret: str | None = None,
    access_token: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    entity_id: str | None = None,
    department_id: str | None = None,
    user_id: str | None = None,
    include_receipts: bool = True,
    lookback_days: int = 30,
    skip_empty_memos: bool = False,
    client: RampClient | None = None,
):
    """Create a dlt source yielding Ramp transactions as markdown documents.

    Args:
        client_id: Ramp OAuth application client ID.
        client_secret: Ramp OAuth application client secret.
        access_token: Direct Ramp API access token.
        from_date: Lower cutoff date (ISO8601).
        to_date: Optional upper cutoff date (ISO8601).
        entity_id: Optional Ramp entity ID filter.
        department_id: Optional department ID filter.
        user_id: Optional user ID filter.
        include_receipts: Whether to fetch receipts and OCR details.
        lookback_days: Lookback window in days to catch delayed memos on older transactions.
        skip_empty_memos: When True, omit transactions that have neither a memo nor receipts.
        client: Optional preconfigured RampClient instance.
    """
    import dlt

    ramp_client = client or RampClient(
        client_id=client_id,
        client_secret=client_secret,
        access_token=access_token,
    )

    @dlt.resource(
        name=RAMP_TABLE_NAME,
        write_disposition="replace",
    )
    def transactions_resource():
        state = dlt.current.resource_state()
        watermark = state.get("last_synced_time")

        # Compute effective from_date with lookback window
        effective_from = from_date
        if not effective_from and watermark:
            try:
                dt = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
                adjusted = dt - timedelta(days=lookback_days)
                effective_from = adjusted.isoformat()
            except Exception:
                effective_from = watermark

        next_page_cursor: str | None = None
        max_seen_time = watermark

        while True:
            resp = ramp_client.list_transactions(
                from_date=effective_from,
                to_date=to_date,
                entity_id=entity_id,
                department_id=department_id,
                user_id=user_id,
                start=next_page_cursor,
            )

            txns = resp.get("data", [])
            for txn in txns:
                txn_time = txn.get("user_transaction_time") or txn.get("created_at")
                if txn_time and (max_seen_time is None or txn_time > max_seen_time):
                    max_seen_time = txn_time

                receipts: list[dict[str, Any]] = []
                if include_receipts:
                    try:
                        receipts = ramp_client.list_receipts(transaction_id=txn.get("id"))
                    except Exception as err:
                        logger.warning("Failed to fetch receipts for %s: %s", txn.get("id"), err)

                memo = (txn.get("memo") or "").strip()
                if skip_empty_memos and not memo and not receipts:
                    continue

                yield _transaction_to_document(txn, receipts)

            page_info = resp.get("page", {})
            next_page_cursor = page_info.get("next")
            if not next_page_cursor:
                break

        if max_seen_time:
            state["last_synced_time"] = max_seen_time

    @dlt.source(name=RAMP_SOURCE_NAME)
    def source():
        return transactions_resource

    created_source = source()
    created_source.cognee_document_source = RAMP_SOURCE_NAME
    setattr(created_source, DOCUMENT_SOURCE_ATTR, RAMP_SOURCE_NAME)
    return created_source
