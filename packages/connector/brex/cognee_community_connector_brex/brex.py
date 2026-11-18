"""DLT source for Brex expenses, memos, and budgets (full-snapshot sync + forget-on-delete).

Fetches Brex corporate card expenses, employee memos, receipt notes, and corporate
budgets, then formats them as markdown documents for cognee's ingestion pipeline.

Unlike the relational dlt path (SQL/CSV), Brex entities are ingested as
*normal documents*: the source declares ``cognee_document_source = "brex"``,
so ``resolve_dlt_sources`` tags each row ``external_metadata["source"] = "brex"``
(not ``"dlt"``). ``is_dlt_sourced`` therefore returns False and each record flows
through the standard cognify entity-extraction pipeline — the right treatment
for expense memos, budget goals, merchant categorization, and financial semantics.

The source defaults to a full snapshot: ``write_disposition="replace"`` rewrites
staging with expenses and budgets currently visible. Deleted or refunded items
drop out of the snapshot, allowing cognee's ``orphan_cleanup`` to remove stale
nodes from the knowledge graph and vector stores.

Incremental sync uses a ``posted_at_start`` watermark stored in persistent dlt state.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from typing import Any

import httpx
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("brex_connector")

BREX_TABLE_NAME = "brex_records"
BREX_SOURCE_NAME = "brex"

_BASE_URL = "https://platform.brexapis.com"
_MAX_RETRIES = 5
_BASE_BACKOFF = 1.0


def _get_retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Calculate exponential retry delay honoring Retry-After headers."""
    if response is not None and "retry-after" in response.headers:
        try:
            delay = float(response.headers["retry-after"])
            if math.isfinite(delay) and delay >= 0:
                return delay
        except (ValueError, TypeError):
            pass
    return _BASE_BACKOFF * (2**attempt)


class BrexClient:
    """Synchronous HTTP client for Brex REST APIs."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = _BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ):
        token = api_key or os.getenv("BREX_API_KEY") or os.getenv("BREX_ACCESS_TOKEN")
        if not token:
            raise ValueError(
                "Brex API key required. Pass api_key or set BREX_API_KEY / BREX_ACCESS_TOKEN."
            )
        self.api_key = token.strip()
        self.base_url = base_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "cognee-community-connector-brex/0.1.0",
        }
        self.client = httpx.Client(
            headers=headers,
            timeout=30.0,
            transport=transport,
        )

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}" if path.startswith("/") else path
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self.client.request(method, url, params=params)
                if resp.status_code in (429, 500, 502, 503, 504):
                    delay = _get_retry_delay(resp, attempt)
                    logger.warning(
                        "Brex API returned %s, retrying in %.2fs",
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
                logger.warning(
                    "Network error calling %s (%s), retrying in %.2fs",
                    url,
                    err,
                    delay,
                )
                time.sleep(delay)

        raise RuntimeError(f"Exceeded max retries calling Brex API: {url}")

    def list_expenses(
        self,
        posted_at_start: str | None = None,
        posted_at_end: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Fetch card expenses from /v2/expenses/card."""
        params: dict[str, Any] = {"limit": limit}
        if posted_at_start:
            params["posted_at_start"] = posted_at_start
        if posted_at_end:
            params["posted_at_end"] = posted_at_end
        if cursor:
            params["cursor"] = cursor

        return self._request("GET", "/v2/expenses/card", params=params)

    def list_budgets(
        self,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Fetch corporate budgets from /v1/budgets."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor

        return self._request("GET", "/v1/budgets", params=params)

    def close(self) -> None:
        """Close underlying HTTP client."""
        self.client.close()


def _expense_to_document(expense: dict[str, Any]) -> dict[str, Any]:
    """Convert a Brex expense into a structured markdown document."""
    exp_id = expense.get("id", "unknown")
    merchant = expense.get("merchant_name") or expense.get("merchant") or "Unknown Merchant"
    amount_info = expense.get("original_amount") or expense.get("amount") or {}
    if isinstance(amount_info, dict):
        amount = amount_info.get("amount", 0)
        currency = amount_info.get("currency", "USD")
    else:
        amount = amount_info
        currency = expense.get("currency", "USD")

    status = expense.get("status", "POSTED")
    posted_at = expense.get("posted_at") or expense.get("created_at") or "N/A"
    category = expense.get("category") or "Uncategorized"
    memo = (expense.get("memo") or "").strip()
    cardholder = expense.get("cardholder") or expense.get("user") or {}
    cardholder_name = (
        cardholder.get("name")
        or cardholder.get("email")
        or (cardholder if isinstance(cardholder, str) else "Unknown Cardholder")
    )

    doc_lines = [
        f"# Brex Expense: {merchant} ({amount} {currency})",
        "",
        f"- **Expense ID**: {exp_id}",
        f"- **Date Posted**: {posted_at}",
        f"- **Cardholder**: {cardholder_name}",
        f"- **Merchant**: {merchant}",
        f"- **Amount**: {amount} {currency}",
        f"- **Category**: {category}",
        f"- **Status**: {status}",
    ]

    if memo:
        doc_lines.extend(["", "## Business Purpose Memo", "", memo])

    receipts = expense.get("receipts") or []
    if receipts:
        doc_lines.extend(["", f"## Receipt Attachments ({len(receipts)})"])
        for idx, r in enumerate(receipts, start=1):
            r_name = r.get("name") or r.get("id", f"Receipt {idx}")
            doc_lines.append(f"- {r_name}")

    full_text = "\n".join(doc_lines)
    content_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()

    return {
        "id": f"brex:expense:{exp_id}",
        "data_id": f"brex:{content_hash}",
        "name": f"{merchant} - {amount} {currency}",
        "text": full_text,
        "type": "expense",
        "amount": amount,
        "currency": currency,
        "posted_at": posted_at,
        "status": status,
        "external_metadata": {
            "source": BREX_SOURCE_NAME,
            "record_type": "expense",
            "expense_id": exp_id,
            "has_memo": bool(memo),
        },
    }


def _budget_to_document(budget: dict[str, Any]) -> dict[str, Any]:
    """Convert a Brex budget into a structured markdown document."""
    b_id = budget.get("id", "unknown")
    name = budget.get("name", "Corporate Budget")
    description = (budget.get("description") or "").strip()
    period = budget.get("period", "MONTHLY")

    limit_info = budget.get("limit") or {}
    if isinstance(limit_info, dict):
        limit_amount = limit_info.get("amount", 0)
        currency = limit_info.get("currency", "USD")
    else:
        limit_amount = limit_info
        currency = "USD"

    spent_info = budget.get("spend") or budget.get("spent") or {}
    spent_amount = spent_info.get("amount", 0) if isinstance(spent_info, dict) else spent_info

    doc_lines = [
        f"# Brex Budget: {name}",
        "",
        f"- **Budget ID**: {b_id}",
        f"- **Period**: {period}",
        f"- **Total Allocation / Limit**: {limit_amount} {currency}",
        f"- **Spent to Date**: {spent_amount} {currency}",
    ]

    if description:
        doc_lines.extend(["", "## Budget Description & Objectives", "", description])

    full_text = "\n".join(doc_lines)
    content_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()

    return {
        "id": f"brex:budget:{b_id}",
        "data_id": f"brex:{content_hash}",
        "name": f"Budget: {name}",
        "text": full_text,
        "type": "budget",
        "external_metadata": {
            "source": BREX_SOURCE_NAME,
            "record_type": "budget",
            "budget_id": b_id,
        },
    }


def brex_source(
    api_key: str | None = None,
    posted_at_start: str | None = None,
    posted_at_end: str | None = None,
    include_expenses: bool = True,
    include_budgets: bool = True,
    client: BrexClient | None = None,
):
    """Create a dlt source yielding Brex expenses and budgets as markdown documents.

    Args:
        api_key: Brex API token or OAuth Bearer token.
        posted_at_start: ISO8601 start timestamp filter for expenses.
        posted_at_end: Optional ISO8601 end timestamp filter for expenses.
        include_expenses: Whether to ingest corporate card expenses and memos.
        include_budgets: Whether to ingest corporate budgets.
        client: Optional preconfigured BrexClient instance.
    """
    import dlt

    brex_client = client or BrexClient(api_key=api_key)

    @dlt.resource(
        name=BREX_TABLE_NAME,
        write_disposition="replace",
    )
    def records_resource():
        state = dlt.current.resource_state()
        watermark = state.get("last_posted_at")
        effective_start = posted_at_start or watermark

        max_seen_posted_at = effective_start

        # 1. Fetch Expenses
        if include_expenses:
            cursor: str | None = None
            while True:
                resp = brex_client.list_expenses(
                    posted_at_start=effective_start,
                    posted_at_end=posted_at_end,
                    cursor=cursor,
                )
                items = resp.get("items") or resp.get("data") or []
                for exp in items:
                    p_at = exp.get("posted_at") or exp.get("created_at")
                    if p_at and (max_seen_posted_at is None or p_at > max_seen_posted_at):
                        max_seen_posted_at = p_at

                    yield _expense_to_document(exp)

                cursor = resp.get("next_cursor") or resp.get("cursor")
                if not cursor:
                    break

        # 2. Fetch Budgets
        if include_budgets:
            b_cursor: str | None = None
            while True:
                b_resp = brex_client.list_budgets(cursor=b_cursor)
                budgets = b_resp.get("items") or b_resp.get("data") or []
                for b in budgets:
                    yield _budget_to_document(b)

                b_cursor = b_resp.get("next_cursor") or b_resp.get("cursor")
                if not b_cursor:
                    break

        if max_seen_posted_at:
            state["last_posted_at"] = max_seen_posted_at

    @dlt.source(name=BREX_SOURCE_NAME)
    def source():
        return records_resource

    created_source = source()
    created_source.cognee_document_source = BREX_SOURCE_NAME
    setattr(created_source, DOCUMENT_SOURCE_ATTR, BREX_SOURCE_NAME)
    return created_source
