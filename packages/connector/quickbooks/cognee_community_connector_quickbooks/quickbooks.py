"""DLT source for QuickBooks Online (invoices, bills, credit memos, and financial notes).

Ingests financial ledgers into Cognee using Document Mode:
1. Formats transactions and line-item memos into semantic Markdown cards for LLM entity
   extraction and Knowledge Graph memory.
2. Supports incremental sync using QuickBooks `Metadata.LastUpdatedTime` cursor.
3. Supports forget-on-delete: deletes upstream emit `_deleted=True` tombstones so Cognee's
   orphan cleanup purges obsolete nodes and embeddings.
4. Includes anti-corruption guardrails: if sweeps return empty or fail unexpectedly, deletion
   tombstone emission is aborted immediately to protect existing graph state.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("quickbooks_connector")
except ImportError:
    import logging

    logger = logging.getLogger("quickbooks_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

from .client import QuickBooksClient

DEFAULT_ID_PREFIX = "quickbooks"
QUICKBOOKS_SOURCE_NAME = "quickbooks"
INVOICES_TABLE = "quickbooks_invoices"
BILLS_TABLE = "quickbooks_bills"
MEMOS_TABLE = "quickbooks_memos"

_EXTRA_HINT = (
    'The QuickBooks connector requires "dlt" and "requests": '
    'pip install "cognee-community-connector-quickbooks"'
)


def _format_currency(amount: float | int | None, currency: str | None = None) -> str:
    """Format an amount and currency for human-readable display."""
    curr = currency or "USD"
    if amount is None:
        return f"$0.00 {curr}"
    try:
        val = float(amount)
        return f"${val:,.2f} {curr}"
    except (ValueError, TypeError):
        return f"{amount} {curr}"


def _get_nested(obj: dict[str, Any], *keys: str, default: Any = "") -> Any:
    """Safely traverse nested dictionary keys."""
    current: Any = obj
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k)
        else:
            return default
    return current if current is not None else default


def _invoice_to_card(invoice: dict[str, Any], id_prefix: str = DEFAULT_ID_PREFIX) -> dict[str, Any]:
    """Render a QuickBooks Invoice into a semantic Markdown card."""
    raw_id = str(invoice.get("Id", ""))
    doc_num = invoice.get("DocNumber") or raw_id
    customer_name = _get_nested(invoice, "CustomerRef", "name") or "Unknown Customer"
    currency = _get_nested(invoice, "CurrencyRef", "value") or "USD"
    total_amt = invoice.get("TotalAmt", 0.0)
    balance = invoice.get("Balance", 0.0)
    txn_date = invoice.get("TxnDate", "")
    due_date = invoice.get("DueDate", "")
    customer_memo = _get_nested(invoice, "CustomerMemo", "value") or ""
    private_note = invoice.get("PrivateNote") or ""

    balance_val = float(balance or 0)
    status = (
        "PAID"
        if balance_val <= 0.0
        else f"OPEN (Balance: {_format_currency(balance, currency)})"
    )

    lines: list[str] = [
        f"# Invoice #{doc_num}: {customer_name}",
        "- **Entity:** Invoice",
        f"- **Customer:** {customer_name}",
        f"- **Total Amount:** {_format_currency(total_amt, currency)}",
        f"- **Payment Status:** {status}",
        f"- **Transaction Date:** {txn_date or 'N/A'}",
        f"- **Due Date:** {due_date or 'N/A'}",
    ]

    if customer_memo:
        lines.extend(["", "## Customer Memo", customer_memo])

    if private_note:
        lines.extend(["", "## Private Notes", private_note])

    # Line items
    raw_lines = invoice.get("Line", [])
    item_lines: list[str] = []
    for item in raw_lines:
        detail_type = item.get("DetailType")
        if detail_type == "SalesItemLineDetail":
            sales_detail = item.get("SalesItemLineDetail", {})
            item_name = _get_nested(sales_detail, "ItemRef", "name") or "Item"
            qty = sales_detail.get("Qty", 1)
            unit_price = sales_detail.get("UnitPrice", 0.0)
            line_amt = item.get("Amount", 0.0)
            desc = item.get("Description") or ""
            desc_str = f" - {desc}" if desc else ""
            item_lines.append(
                f"- **{item_name}** (Qty: {qty}, Unit Price: "
                f"{_format_currency(unit_price, currency)}): "
                f"{_format_currency(line_amt, currency)}{desc_str}"
            )
        elif detail_type == "DescriptionOnly":
            desc = item.get("Description") or ""
            if desc:
                item_lines.append(f"- Note: {desc}")

    if item_lines:
        lines.extend(["", "## Line Items", *item_lines])

    return {
        "id": f"{id_prefix}:invoice:{raw_id}",
        "qbo_id": raw_id,
        "title": f"Invoice #{doc_num} - {customer_name}",
        "url": f"https://app.qbo.intuit.com/app/invoice?txnId={raw_id}",
        "content": "\n".join(lines),
        "_deleted": False,
    }


def _bill_to_card(bill: dict[str, Any], id_prefix: str = DEFAULT_ID_PREFIX) -> dict[str, Any]:
    """Render a QuickBooks Bill into a semantic Markdown card."""
    raw_id = str(bill.get("Id", ""))
    doc_num = bill.get("DocNumber") or raw_id
    vendor_name = _get_nested(bill, "VendorRef", "name") or "Unknown Vendor"
    currency = _get_nested(bill, "CurrencyRef", "value") or "USD"
    total_amt = bill.get("TotalAmt", 0.0)
    balance = bill.get("Balance", 0.0)
    txn_date = bill.get("TxnDate", "")
    due_date = bill.get("DueDate", "")
    private_note = bill.get("PrivateNote") or ""
    ap_account = _get_nested(bill, "APAccountRef", "name") or "Accounts Payable"

    balance_val = float(balance or 0)
    status = (
        "PAID"
        if balance_val <= 0.0
        else f"UNPAID (Balance: {_format_currency(balance, currency)})"
    )

    lines: list[str] = [
        f"# Bill #{doc_num}: {vendor_name}",
        "- **Entity:** Vendor Bill",
        f"- **Vendor:** {vendor_name}",
        f"- **Total Amount:** {_format_currency(total_amt, currency)}",
        f"- **Payment Status:** {status}",
        f"- **AP Account:** {ap_account}",
        f"- **Transaction Date:** {txn_date or 'N/A'}",
        f"- **Due Date:** {due_date or 'N/A'}",
    ]

    if private_note:
        lines.extend(["", "## Bill Memo / Private Notes", private_note])

    raw_lines = bill.get("Line", [])
    item_lines: list[str] = []
    for item in raw_lines:
        detail_type = item.get("DetailType")
        line_amt = item.get("Amount", 0.0)
        desc = item.get("Description") or ""
        if detail_type == "AccountBasedExpenseLineDetail":
            account_detail = item.get("AccountBasedExpenseLineDetail", {})
            acc_name = _get_nested(account_detail, "AccountRef", "name") or "Expense Account"
            desc_str = f" - {desc}" if desc else ""
            item_lines.append(
                f"- **{acc_name}**: {_format_currency(line_amt, currency)}{desc_str}"
            )
        elif detail_type == "ItemBasedExpenseLineDetail":
            item_detail = item.get("ItemBasedExpenseLineDetail", {})
            item_name = _get_nested(item_detail, "ItemRef", "name") or "Item"
            qty = item_detail.get("Qty", 1)
            desc_str = f" - {desc}" if desc else ""
            item_lines.append(
                f"- **{item_name}** (Qty: {qty}): {_format_currency(line_amt, currency)}{desc_str}"
            )

    if item_lines:
        lines.extend(["", "## Expense Items", *item_lines])

    return {
        "id": f"{id_prefix}:bill:{raw_id}",
        "qbo_id": raw_id,
        "title": f"Bill #{doc_num} - {vendor_name}",
        "url": f"https://app.qbo.intuit.com/app/bill?txnId={raw_id}",
        "content": "\n".join(lines),
        "_deleted": False,
    }


def _credit_memo_to_card(
    memo: dict[str, Any],
    id_prefix: str = DEFAULT_ID_PREFIX,
) -> dict[str, Any]:
    """Render a QuickBooks CreditMemo or VendorCredit into a semantic Markdown card."""
    raw_id = str(memo.get("Id", ""))
    doc_num = memo.get("DocNumber") or raw_id
    party_name = (
        _get_nested(memo, "CustomerRef", "name")
        or _get_nested(memo, "VendorRef", "name")
        or "Unknown Party"
    )
    currency = _get_nested(memo, "CurrencyRef", "value") or "USD"
    total_amt = memo.get("TotalAmt", 0.0)
    balance = memo.get("RemainingCredit") or memo.get("Balance", 0.0)
    txn_date = memo.get("TxnDate", "")
    customer_memo = _get_nested(memo, "CustomerMemo", "value") or memo.get("PrivateNote") or ""

    lines: list[str] = [
        f"# Credit Memo #{doc_num}: {party_name}",
        "- **Entity:** Credit Memo",
        f"- **Party:** {party_name}",
        f"- **Total Amount:** {_format_currency(total_amt, currency)}",
        f"- **Remaining Balance:** {_format_currency(balance, currency)}",
        f"- **Transaction Date:** {txn_date or 'N/A'}",
    ]

    if customer_memo:
        lines.extend(["", "## Memo / Notes", customer_memo])

    return {
        "id": f"{id_prefix}:creditmemo:{raw_id}",
        "qbo_id": raw_id,
        "title": f"Credit Memo #{doc_num} - {party_name}",
        "url": f"https://app.qbo.intuit.com/app/creditmemo?txnId={raw_id}",
        "content": "\n".join(lines),
        "_deleted": False,
    }


def quickbooks_source(
    client_id: str | None = None,
    client_secret: str | None = None,
    refresh_token: str | None = None,
    access_token: str | None = None,
    realm_id: str | None = None,
    sandbox: bool = True,
    entities: list[str] | None = None,
    id_prefix: str = DEFAULT_ID_PREFIX,
    client: Any = None,
):
    """Return a dlt source yielding QuickBooks Online transactions for ``cognee.remember``.

    Args:
        client_id: Intuit OAuth 2.0 Client ID (defaults to env QB_CLIENT_ID).
        client_secret: Intuit OAuth 2.0 Client Secret (defaults to env QB_CLIENT_SECRET).
        refresh_token: OAuth 2.0 Refresh Token (defaults to env QB_REFRESH_TOKEN).
        access_token: Optional active bearer token (defaults to env QB_ACCESS_TOKEN).
        realm_id: QuickBooks Online Company ID (defaults to env QB_REALM_ID).
        sandbox: Use Intuit developer sandbox if True, production if False.
        entities: List of entities to sync, e.g. ["Invoice", "Bill", "CreditMemo"].
        id_prefix: Namespace prefix for document IDs (default: "quickbooks").
        client: Pre-configured QuickBooksClient instance (useful for hermetic unit testing).

    Returns:
        dlt source tagged with document mode for Cognee entity extraction.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    active_client = client or QuickBooksClient(
        realm_id=realm_id,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        access_token=access_token,
        sandbox=sandbox,
    )

    sync_entities = set(entities or ["Invoice", "Bill", "CreditMemo"])

    def _sync_resource(
        entity_name: str,
        table_name: str,
        to_card_fn: Any,
    ):
        @dlt.resource(
            name=table_name,
            primary_key="id",
            write_disposition="merge",
            columns={
                "id": {"data_type": "text"},
                "qbo_id": {"data_type": "text"},
                "title": {"data_type": "text"},
                "url": {"data_type": "text"},
                "content": {"data_type": "text"},
                "_deleted": {"data_type": "bool", "hard_delete": True},
            },
        )
        def resource_func() -> Iterator[dict[str, Any]]:
            state = dlt.current.resource_state()
            last_updated = state.get("last_updated_time")
            known_ids = set(state.get("known_ids", []))

            current_sync_ids: set[str] = set()
            newest_updated = last_updated
            records_fetched = 0

            try:
                for item in active_client.fetch_all(entity_name, last_updated_time=last_updated):
                    card = to_card_fn(item, id_prefix=id_prefix)
                    raw_id = str(item.get("Id", ""))
                    current_sync_ids.add(raw_id)
                    records_fetched += 1

                    meta = item.get("MetaData") or {}
                    last_mod = meta.get("LastUpdatedTime")
                    if last_mod and (newest_updated is None or last_mod > newest_updated):
                        newest_updated = last_mod

                    yield card

                # ── Deletion reconciliation ─────────────────────────────────
                # 1. CDC API (if last_updated is known)
                if last_updated:
                    try:
                        cdc_records = active_client.fetch_cdc(
                            [entity_name], changed_since=last_updated
                        )
                        for cdc_item in cdc_records:
                            if cdc_item.get("status") == "Deleted":
                                del_id = str(cdc_item.get("Id", ""))
                                yield {
                                    "id": f"{id_prefix}:{entity_name.lower()}:{del_id}",
                                    "_deleted": True,
                                }
                                known_ids.discard(del_id)
                    except Exception as cdc_exc:
                        logger.warning(
                            "QuickBooks CDC query failed, relying on cursor state: %s", cdc_exc
                        )

                # 2. Sweep reconciliation (on full backfill scan when last_updated is None)
                if not last_updated:
                    # Anti-corruption guardrail: if 0 records fetched on known_ids, abort
                    if records_fetched == 0 and known_ids:
                        logger.warning(
                            "QuickBooks %s sweep returned 0 records but %d were known. "
                            "Aborting deletion reconciliation to prevent accidental graph wipe.",
                            entity_name,
                            len(known_ids),
                        )
                        return

                    deleted_ids = known_ids - current_sync_ids
                    for d_id in sorted(deleted_ids):
                        yield {
                            "id": f"{id_prefix}:{entity_name.lower()}:{d_id}",
                            "_deleted": True,
                        }
                    state["known_ids"] = sorted(current_sync_ids)
                else:
                    # Add current sync ids to known ids
                    known_ids.update(current_sync_ids)
                    state["known_ids"] = sorted(known_ids)

                if newest_updated:
                    state["last_updated_time"] = newest_updated

                logger.info(
                    "QuickBooks %s: synced %d record(s), cursor advanced to %s",
                    entity_name,
                    records_fetched,
                    newest_updated,
                )

            except Exception as exc:
                logger.error("QuickBooks %s sync failed: %s. Aborting.", entity_name, exc)
                raise

        return resource_func

    resources = []
    if "Invoice" in sync_entities:
        resources.append(_sync_resource("Invoice", INVOICES_TABLE, _invoice_to_card))
    if "Bill" in sync_entities:
        resources.append(_sync_resource("Bill", BILLS_TABLE, _bill_to_card))
    if "CreditMemo" in sync_entities:
        resources.append(_sync_resource("CreditMemo", MEMOS_TABLE, _credit_memo_to_card))

    @dlt.source(name=QUICKBOOKS_SOURCE_NAME)
    def _quickbooks():
        return resources

    source = _quickbooks()
    # Tag source for Cognee Document Mode (routes through cognify Knowledge Graph pipeline)
    setattr(source, DOCUMENT_SOURCE_ATTR, QUICKBOOKS_SOURCE_NAME)
    return source
