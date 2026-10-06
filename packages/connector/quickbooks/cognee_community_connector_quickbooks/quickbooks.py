"""DLT source for QuickBooks Online (invoices, bills, and memos) with forget-on-delete.

Syncs QuickBooks Online accounting data into Cognee memory for financial reasoning and agent search.

Architecture & Design:
---------------------
- **Document Mode**: Configured with ``DOCUMENT_SOURCE_ATTR = "quickbooks"`` so Cognee's
  ``resolve_dlt_sources`` routes transactions through the ``cognify`` entity-extraction and
  knowledge graph pipeline.
- **Intuit OAuth 2.0 & Company Boundary**: Supports direct access tokens and automatic refresh
  via Intuit OAuth 2.0 token endpoint. Every record is strictly scoped to the company ``realm_id``.
  Supports both sandbox (``https://sandbox-quickbooks.api.intuit.com``) and production endpoints.
- **Entity Ingestion**: Ingests Invoices, Bills, and Credit Memos (including transaction memos and
  line item context), formatting each into descriptive natural-language markdown for AI retrieval.
- **Incremental Sync**: Queries use ``MetaData.LastUpdatedTime > '...'`` and persist the cursor in
  dlt's per-resource state so subsequent syncs fetch only new or changed accounting transactions.
- **Full-Snapshot Sync & Forget-on-Delete**: Resources use ``write_disposition="replace"``. Voided
  or deleted transactions upstream drop out of staging, prompting Cognee's ``orphan_cleanup`` to
  purge them from graph and vector stores.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("quickbooks_connector")

QUICKBOOKS_SOURCE_NAME = "quickbooks"
QUICKBOOKS_INVOICES_TABLE = "quickbooks_invoices"
QUICKBOOKS_BILLS_TABLE = "quickbooks_bills"
QUICKBOOKS_MEMOS_TABLE = "quickbooks_memos"

_SANDBOX_BASE_URL = "https://sandbox-quickbooks.api.intuit.com/v3/company"
_PROD_BASE_URL = "https://quickbooks.api.intuit.com/v3/company"
_INTUIT_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

_MAX_RETRIES = 5
_EXTRA_HINT = (
    "The QuickBooks connector requires dlt and httpx: "
    'pip install "cognee-community-connector-quickbooks" (provides dlt and httpx).'
)


class QuickBooksClient:
    """HTTP client for QuickBooks Online Accounting API v3 with OAuth 2.0 refresh support."""

    def __init__(
        self,
        realm_id: str,
        access_token: str | None = None,
        refresh_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        environment: str = "sandbox",
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        import httpx

        self.realm_id = realm_id
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.environment = environment.lower()

        if base_url:
            self.base_url = base_url.rstrip("/")
        elif self.environment == "production":
            self.base_url = f"{_PROD_BASE_URL}/{realm_id}"
        else:
            self.base_url = f"{_SANDBOX_BASE_URL}/{realm_id}"

        self.client = httpx.Client(
            headers={
                "Accept": "application/json",
                "User-Agent": "cognee-community-connector-quickbooks/0.1.0",
            },
            timeout=timeout,
        )

        if not self.access_token and self.refresh_token and self.client_id and self.client_secret:
            self.refresh_access_token()

    def close(self) -> None:
        self.client.close()

    def refresh_access_token(self) -> str:
        """Exchange refresh token for a new access token via Intuit OAuth 2.0 endpoint."""
        import httpx

        if not (self.refresh_token and self.client_id and self.client_secret):
            raise ValueError(
                "Cannot refresh token without refresh_token, client_id, and client_secret."
            )

        response = httpx.post(
            _INTUIT_TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        data = response.json()
        self.access_token = data.get("access_token")
        if data.get("refresh_token"):
            self.refresh_token = data.get("refresh_token")
        return self.access_token

    def query(self, entity_name: str, query_str: str) -> list[dict[str, Any]]:
        """Run an Intuit SQL query against /query endpoint with auto-pagination and retry."""
        results: list[dict[str, Any]] = []
        start_position = 1
        page_size = 100

        while True:
            paged_query = f"{query_str} STARTPOSITION {start_position} MAXRESULTS {page_size}"
            params = {"query": paged_query}
            data = self._get("query", params=params)

            query_response = data.get("QueryResponse", {})
            entities = query_response.get(entity_name, [])
            if not entities:
                break

            results.extend(entities)
            if len(entities) < page_size:
                break

            start_position += len(entities)

        return results

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Issue an authenticated GET with exponential backoff on 429 and transient errors."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        params = params or {}

        for attempt in range(_MAX_RETRIES):
            headers = {"Authorization": f"Bearer {self.access_token}"}
            try:
                response = self.client.get(url, params=params, headers=headers)
                if response.status_code == 401 and self.refresh_token:
                    # Token expired, attempt refresh once
                    logger.info("QuickBooks 401 Unauthorized — refreshing OAuth token.")
                    self.refresh_access_token()
                    headers["Authorization"] = f"Bearer {self.access_token}"
                    response = self.client.get(url, params=params, headers=headers)

                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt == _MAX_RETRIES - 1:
                        response.raise_for_status()
                    delay = _retry_after(response.headers, attempt)
                    logger.warning(
                        "QuickBooks API HTTP %d on %s — retrying in %.1fs (%d/%d).",
                        response.status_code,
                        url,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue

                response.raise_for_status()
                return response.json()
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                    raise
                delay = float(2**attempt)
                logger.warning(
                    "QuickBooks API error on %s (%s) — retrying in %.1fs (%d/%d).",
                    url,
                    exc,
                    delay,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(delay)

        raise RuntimeError(f"Exhausted retries for QuickBooks API endpoint: {url}")


def _is_transient(exc: Exception) -> bool:
    """Return True if exception is transient network or timeout error."""
    import httpx

    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


def _retry_after(headers: Any, attempt: int) -> float:
    """Extract Retry-After header or calculate exponential backoff."""
    header = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    if header:
        try:
            return float(header)
        except (ValueError, TypeError):
            pass
    return float(2**attempt)


def quickbooks_source(
    realm_id: str | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    environment: str = "sandbox",
    base_url: str | None = None,
    include_invoices: bool = True,
    include_bills: bool = True,
    include_memos: bool = True,
    invoice_ids: list[str] | None = None,
    bill_ids: list[str] | None = None,
    memo_ids: list[str] | None = None,
    since: str | None = None,
    client: Any = None,
) -> Any:
    """Create a DLT source yielding QuickBooks invoices, bills, and memos as Cognee documents.

    Args:
        realm_id: QuickBooks company realmId. Falls back to ``QUICKBOOKS_REALM_ID``.
        access_token: Intuit OAuth access token. Falls back to ``QUICKBOOKS_ACCESS_TOKEN``.
        refresh_token: Intuit OAuth refresh token. Falls back to ``QUICKBOOKS_REFRESH_TOKEN``.
        client_id: Intuit app Client ID. Falls back to ``QUICKBOOKS_CLIENT_ID``.
        client_secret: Intuit app Client Secret. Falls back to ``QUICKBOOKS_CLIENT_SECRET``.
        environment: API environment, ``"sandbox"`` (default) or ``"production"``.
        base_url: Optional override URL for testing or proxying.
        include_invoices: Whether to ingest invoices (default: True).
        include_bills: Whether to ingest bills (default: True).
        include_memos: Whether to ingest credit memos / memos (default: True).
        invoice_ids: Optional list of specific Invoice IDs to ingest.
        bill_ids: Optional list of specific Bill IDs to ingest.
        memo_ids: Optional list of specific CreditMemo IDs to ingest.
        since: Optional ISO-8601 timestamp string for incremental sync.
        client: Optional pre-built QuickBooks client for test injection.

    Returns:
        A DLT source ready to pass to ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    resolved_client = client
    if resolved_client is None:
        resolved_realm_id = realm_id or os.environ.get("QUICKBOOKS_REALM_ID")
        if not resolved_realm_id:
            raise ValueError(
                "QuickBooks realmId required: pass realm_id= parameter or set QUICKBOOKS_REALM_ID."
            )

        resolved_token = access_token or os.environ.get("QUICKBOOKS_ACCESS_TOKEN")
        resolved_refresh_token = refresh_token or os.environ.get("QUICKBOOKS_REFRESH_TOKEN")
        resolved_client_id = client_id or os.environ.get("QUICKBOOKS_CLIENT_ID")
        resolved_client_secret = client_secret or os.environ.get("QUICKBOOKS_CLIENT_SECRET")

        if not resolved_token and not (
            resolved_refresh_token and resolved_client_id and resolved_client_secret
        ):
            raise ValueError(
                "QuickBooks authentication required: pass access_token or provide "
                "refresh_token, client_id, and client_secret for OAuth 2.0 refresh."
            )

        resolved_client = QuickBooksClient(
            realm_id=resolved_realm_id,
            access_token=resolved_token,
            refresh_token=resolved_refresh_token,
            client_id=resolved_client_id,
            client_secret=resolved_client_secret,
            environment=environment,
            base_url=base_url,
        )

    company_id = resolved_client.realm_id
    resources = []

    if include_invoices:

        @dlt.resource(
            name=QUICKBOOKS_INVOICES_TABLE,
            primary_key="id",
            write_disposition="replace",
        )
        def quickbooks_invoices() -> Iterator[dict[str, Any]]:
            """Yield invoices as Cognee documents."""
            try:
                state = dlt.current.resource_state()
            except Exception:
                state = {}

            effective_since = since or state.get("last_updated_time")
            newest_time = effective_since
            query = "SELECT * FROM Invoice"
            if effective_since:
                query += f" WHERE MetaData.LastUpdatedTime > '{effective_since}'"

            count = 0
            for inv in resolved_client.query("Invoice", query):
                inv_id = str(inv.get("Id") or "")
                if invoice_ids and inv_id not in invoice_ids:
                    continue

                doc = _invoice_to_document(company_id, inv)
                if doc:
                    count += 1
                    updated_time = (inv.get("MetaData") or {}).get("LastUpdatedTime") or ""
                    if updated_time and (not newest_time or updated_time > newest_time):
                        newest_time = updated_time
                    yield doc

            if newest_time:
                state["last_updated_time"] = newest_time
            logger.info("QuickBooks: synced %d invoice(s).", count)

        resources.append(quickbooks_invoices)

    if include_bills:

        @dlt.resource(
            name=QUICKBOOKS_BILLS_TABLE,
            primary_key="id",
            write_disposition="replace",
        )
        def quickbooks_bills() -> Iterator[dict[str, Any]]:
            """Yield bills as Cognee documents."""
            try:
                state = dlt.current.resource_state()
            except Exception:
                state = {}

            effective_since = since or state.get("last_updated_time")
            newest_time = effective_since
            query = "SELECT * FROM Bill"
            if effective_since:
                query += f" WHERE MetaData.LastUpdatedTime > '{effective_since}'"

            count = 0
            for bill in resolved_client.query("Bill", query):
                bill_id = str(bill.get("Id") or "")
                if bill_ids and bill_id not in bill_ids:
                    continue

                doc = _bill_to_document(company_id, bill)
                if doc:
                    count += 1
                    updated_time = (bill.get("MetaData") or {}).get("LastUpdatedTime") or ""
                    if updated_time and (not newest_time or updated_time > newest_time):
                        newest_time = updated_time
                    yield doc

            if newest_time:
                state["last_updated_time"] = newest_time
            logger.info("QuickBooks: synced %d bill(s).", count)

        resources.append(quickbooks_bills)

    if include_memos:

        @dlt.resource(
            name=QUICKBOOKS_MEMOS_TABLE,
            primary_key="id",
            write_disposition="replace",
        )
        def quickbooks_memos() -> Iterator[dict[str, Any]]:
            """Yield credit memos as Cognee documents."""
            try:
                state = dlt.current.resource_state()
            except Exception:
                state = {}

            effective_since = since or state.get("last_updated_time")
            newest_time = effective_since
            query = "SELECT * FROM CreditMemo"
            if effective_since:
                query += f" WHERE MetaData.LastUpdatedTime > '{effective_since}'"

            count = 0
            for memo in resolved_client.query("CreditMemo", query):
                memo_id = str(memo.get("Id") or "")
                if memo_ids and memo_id not in memo_ids:
                    continue

                doc = _credit_memo_to_document(company_id, memo)
                if doc:
                    count += 1
                    updated_time = (memo.get("MetaData") or {}).get("LastUpdatedTime") or ""
                    if updated_time and (not newest_time or updated_time > newest_time):
                        newest_time = updated_time
                    yield doc

            if newest_time:
                state["last_updated_time"] = newest_time
            logger.info("QuickBooks: synced %d memo(s).", count)

        resources.append(quickbooks_memos)

    @dlt.source(name=QUICKBOOKS_SOURCE_NAME)
    def _source() -> list[Any]:
        return [res() for res in resources]

    src = _source()
    setattr(src, DOCUMENT_SOURCE_ATTR, QUICKBOOKS_SOURCE_NAME)
    return src


# ---------------------------------------------------------------------------
# Document Transformers
# ---------------------------------------------------------------------------


def _invoice_to_document(realm_id: str, invoice: dict[str, Any]) -> dict[str, Any] | None:
    """Format an Invoice into a Cognee document row."""
    inv_id = str(invoice.get("Id") or "")
    if not inv_id:
        return None

    doc_number = invoice.get("DocNumber") or f"INV-{inv_id}"
    customer = (invoice.get("CustomerRef") or {}).get("name") or "Unknown Customer"
    total_amount = invoice.get("TotalAmt", 0.0)
    balance = invoice.get("Balance", 0.0)
    txn_date = invoice.get("TxnDate") or "Not specified"
    due_date = invoice.get("DueDate") or "Not specified"
    currency = (invoice.get("CurrencyRef") or {}).get("value") or "USD"
    customer_memo = (invoice.get("CustomerMemo") or {}).get("value") or ""
    private_note = invoice.get("PrivateNote") or ""

    lines = [
        f"# Invoice: {doc_number}",
        "",
        f"- **Invoice ID**: {inv_id}",
        f"- **Company Realm**: {realm_id}",
        f"- **Customer**: {customer}",
        f"- **Issue Date**: {txn_date}",
        f"- **Due Date**: {due_date}",
        f"- **Total Amount**: {total_amount} {currency}",
        f"- **Remaining Balance**: {balance} {currency}",
    ]

    if customer_memo:
        lines.append(f"- **Customer Memo**: {customer_memo}")
    if private_note:
        lines.append(f"- **Private Note**: {private_note}")

    line_items = _render_line_items(invoice.get("Line", []))
    if line_items:
        lines.extend(["", "## Line Items", "", line_items])

    return {
        "id": f"quickbooks:{realm_id}:invoice:{inv_id}",
        "url": f"https://app.qbo.intuit.com/app/invoice?txnId={inv_id}",
        "title": f"Invoice {doc_number} - {customer} ({total_amount} {currency})",
        "content": "\n".join(lines),
    }


def _bill_to_document(realm_id: str, bill: dict[str, Any]) -> dict[str, Any] | None:
    """Format a Bill into a Cognee document row."""
    bill_id = str(bill.get("Id") or "")
    if not bill_id:
        return None

    doc_number = bill.get("DocNumber") or f"BILL-{bill_id}"
    vendor = (bill.get("VendorRef") or {}).get("name") or "Unknown Vendor"
    total_amount = bill.get("TotalAmt", 0.0)
    balance = bill.get("Balance", 0.0)
    txn_date = bill.get("TxnDate") or "Not specified"
    due_date = bill.get("DueDate") or "Not specified"
    currency = (bill.get("CurrencyRef") or {}).get("value") or "USD"
    private_note = bill.get("PrivateNote") or ""

    lines = [
        f"# Bill: {doc_number}",
        "",
        f"- **Bill ID**: {bill_id}",
        f"- **Company Realm**: {realm_id}",
        f"- **Vendor**: {vendor}",
        f"- **Transaction Date**: {txn_date}",
        f"- **Due Date**: {due_date}",
        f"- **Total Amount**: {total_amount} {currency}",
        f"- **Remaining Balance**: {balance} {currency}",
    ]

    if private_note:
        lines.append(f"- **Private Memo / Note**: {private_note}")

    line_items = _render_line_items(bill.get("Line", []))
    if line_items:
        lines.extend(["", "## Line Items", "", line_items])

    return {
        "id": f"quickbooks:{realm_id}:bill:{bill_id}",
        "url": f"https://app.qbo.intuit.com/app/bill?txnId={bill_id}",
        "title": f"Bill {doc_number} - {vendor} ({total_amount} {currency})",
        "content": "\n".join(lines),
    }


def _credit_memo_to_document(realm_id: str, memo: dict[str, Any]) -> dict[str, Any] | None:
    """Format a CreditMemo into a Cognee document row."""
    memo_id = str(memo.get("Id") or "")
    if not memo_id:
        return None

    doc_number = memo.get("DocNumber") or f"CM-{memo_id}"
    customer = (memo.get("CustomerRef") or {}).get("name") or "Unknown Customer"
    total_amount = memo.get("TotalAmt", 0.0)
    remaining_credit = memo.get("RemainingCredit", 0.0)
    txn_date = memo.get("TxnDate") or "Not specified"
    customer_memo = (memo.get("CustomerMemo") or {}).get("value") or ""
    private_note = memo.get("PrivateNote") or ""

    lines = [
        f"# Credit Memo: {doc_number}",
        "",
        f"- **Credit Memo ID**: {memo_id}",
        f"- **Company Realm**: {realm_id}",
        f"- **Customer**: {customer}",
        f"- **Transaction Date**: {txn_date}",
        f"- **Total Credit Amount**: {total_amount}",
        f"- **Remaining Credit**: {remaining_credit}",
    ]

    if customer_memo:
        lines.append(f"- **Customer Memo**: {customer_memo}")
    if private_note:
        lines.append(f"- **Private Note**: {private_note}")

    line_items = _render_line_items(memo.get("Line", []))
    if line_items:
        lines.extend(["", "## Line Items", "", line_items])

    return {
        "id": f"quickbooks:{realm_id}:creditmemo:{memo_id}",
        "url": f"https://app.qbo.intuit.com/app/creditmemo?txnId={memo_id}",
        "title": f"Credit Memo {doc_number} - {customer} ({total_amount})",
        "content": "\n".join(lines),
    }


def _render_line_items(lines: list[dict[str, Any]]) -> str:
    """Render transaction line items to markdown."""
    rendered: list[str] = []
    for line in lines:
        detail_type = line.get("DetailType")
        if detail_type == "SubTotalLineDetail":
            continue

        desc = line.get("Description") or ""
        amount = line.get("Amount", 0.0)
        item_ref = (
            (line.get("SalesItemLineDetail") or {}).get("ItemRef")
            or (line.get("ItemBasedExpenseLineDetail") or {}).get("ItemRef")
            or {}
        )
        item_name = item_ref.get("name") or "Item"
        qty = (
            (line.get("SalesItemLineDetail") or {}).get("Qty")
            or (line.get("ItemBasedExpenseLineDetail") or {}).get("Qty")
            or 1
        )

        item_str = f"- **{item_name}**"
        if desc:
            item_str += f": {desc}"
        item_str += f" | Qty: {qty} | Amount: {amount}"
        rendered.append(item_str)

    return "\n".join(rendered)
