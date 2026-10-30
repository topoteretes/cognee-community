"""DLT source for QuickBooks Online (invoices, bills, credit memos, and financial notes).

Ingests financial ledgers into Cognee using Document Mode:
1. Formats transactions and line-item memos into semantic Markdown cards for LLM entity
   extraction and Knowledge Graph memory.
2. Supports incremental sync using QuickBooks `Metadata.LastUpdatedTime` cursor.
3. Supports resilient forget-on-delete: combines CDC API and confirmed active-ID sweeps to ensure
   stale records are never left behind if CDC fails or returns incomplete results.
4. Includes anti-corruption guardrails: if sweeps return empty or fail unexpectedly, deletion
   tombstone emission is aborted immediately to protect existing graph state.
5. Realm and resource isolation: uses `PIPELINE_SCOPE_ATTR` and realm-namespaced IDs to avoid
   cross-company state or cursor collision.
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
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"
    PIPELINE_SCOPE_ATTR = "cognee_pipeline_scope"

from .client import QuickBooksClient

DEFAULT_ID_PREFIX = "quickbooks"
QUICKBOOKS_SOURCE_NAME = "quickbooks"
INVOICES_TABLE = "quickbooks_invoices"
BILLS_TABLE = "quickbooks_bills"
MEMOS_TABLE = "quickbooks_memos"

SUPPORTED_ENTITIES = ("Invoice", "Bill", "CreditMemo")

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
        if val < 0:
            return f"-${abs(val):,.2f} {curr}"
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


def _invoice_to_card(
    invoice: dict[str, Any],
    id_prefix: str = DEFAULT_ID_PREFIX,
    include_private_notes: bool = False,
) -> dict[str, Any]:
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
        "PAID" if balance_val <= 0.0 else f"OPEN (Balance: {_format_currency(balance, currency)})"
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

    if include_private_notes and private_note:
        lines.extend(["", "## Private Notes", private_note])

    # Extract sales line items
    raw_lines = invoice.get("Line", [])
    item_rows: list[str] = []
    for line in raw_lines:
        detail_type = line.get("DetailType", "")
        amt = line.get("Amount")
        desc = line.get("Description") or ""

        if detail_type == "SalesItemLineDetail":
            item_detail = line.get("SalesItemLineDetail", {})
            item_name = _get_nested(item_detail, "ItemRef", "name") or desc or "Item"
            unit_price = item_detail.get("UnitPrice")
            qty = item_detail.get("Qty", 1)

            entry = f"- **{item_name}**"
            if unit_price is not None:
                entry += f" - {qty} @ {_format_currency(unit_price, currency)}"
            if amt is not None:
                entry += f" (Total: {_format_currency(amt, currency)})"
            if desc and desc != item_name:
                entry += f" — *{desc}*"
            item_rows.append(entry)
        elif detail_type == "SubTotalLineDetail":
            continue
        elif desc or amt:
            fallback_label = desc or "Line item"
            item_rows.append(f"- {fallback_label}: {_format_currency(amt, currency)}")

    if item_rows:
        lines.extend(["", "## Line Items", *item_rows])

    return {
        "id": f"{id_prefix}:invoice:{raw_id}",
        "qbo_id": raw_id,
        "title": f"Invoice #{doc_num} - {customer_name}",
        "url": f"https://app.qbo.intuit.com/app/invoice?txnId={raw_id}",
        "content": "\n".join(lines),
        "_deleted": False,
    }


def _bill_to_card(
    bill: dict[str, Any],
    id_prefix: str = DEFAULT_ID_PREFIX,
    include_private_notes: bool = False,
) -> dict[str, Any]:
    """Render a QuickBooks Vendor Bill into a semantic Markdown card."""
    raw_id = str(bill.get("Id", ""))
    doc_num = bill.get("DocNumber") or raw_id
    vendor_name = _get_nested(bill, "VendorRef", "name") or "Unknown Vendor"
    currency = _get_nested(bill, "CurrencyRef", "value") or "USD"
    total_amt = bill.get("TotalAmt", 0.0)
    balance = bill.get("Balance", 0.0)
    txn_date = bill.get("TxnDate", "")
    due_date = bill.get("DueDate", "")
    ap_account = _get_nested(bill, "APAccountRef", "name") or ""
    private_note = bill.get("PrivateNote") or ""

    balance_val = float(balance or 0)
    status = (
        "PAID" if balance_val <= 0.0 else f"UNPAID (Balance: {_format_currency(balance, currency)})"
    )

    lines: list[str] = [
        f"# Vendor Bill #{doc_num}: {vendor_name}",
        "- **Entity:** Vendor Bill",
        f"- **Vendor:** {vendor_name}",
        f"- **Total Amount:** {_format_currency(total_amt, currency)}",
        f"- **Status:** {status}",
        f"- **Bill Date:** {txn_date or 'N/A'}",
        f"- **Due Date:** {due_date or 'N/A'}",
    ]
    if ap_account:
        lines.append(f"- **A/P Account:** {ap_account}")

    if include_private_notes and private_note:
        lines.extend(["", "## Notes", private_note])

    raw_lines = bill.get("Line", [])
    item_rows: list[str] = []
    for line in raw_lines:
        detail_type = line.get("DetailType", "")
        amt = line.get("Amount")
        desc = line.get("Description") or ""

        if detail_type in ("AccountBasedExpenseLineDetail", "ItemBasedExpenseLineDetail"):
            detail_obj = line.get(detail_type, {})
            acct_or_item = (
                _get_nested(detail_obj, "AccountRef", "name")
                or _get_nested(detail_obj, "ItemRef", "name")
                or desc
                or "Expense Line"
            )
            entry = f"- **{acct_or_item}**: {_format_currency(amt, currency)}"
            if desc and desc != acct_or_item:
                entry += f" ({desc})"
            item_rows.append(entry)
        elif desc or amt:
            item_rows.append(f"- {desc or 'Expense'}: {_format_currency(amt, currency)}")

    if item_rows:
        lines.extend(["", "## Line Items", *item_rows])

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
    include_private_notes: bool = False,
) -> dict[str, Any]:
    """Render a QuickBooks Credit Memo into a semantic Markdown card."""
    raw_id = str(memo.get("Id", ""))
    doc_num = memo.get("DocNumber") or raw_id
    party_name = _get_nested(memo, "CustomerRef", "name") or "Unknown Party"
    currency = _get_nested(memo, "CurrencyRef", "value") or "USD"
    total_amt = memo.get("TotalAmt", 0.0)
    remaining_credit = memo.get("RemainingCredit", 0.0)
    txn_date = memo.get("TxnDate", "")
    customer_memo = _get_nested(memo, "CustomerMemo", "value") or ""
    private_note = memo.get("PrivateNote") or ""

    lines: list[str] = [
        f"# Credit Memo #{doc_num}: {party_name}",
        "- **Entity:** Credit Memo",
        f"- **Customer/Party:** {party_name}",
        f"- **Total Credit:** {_format_currency(total_amt, currency)}",
        f"- **Remaining Credit:** {_format_currency(remaining_credit, currency)}",
        f"- **Date:** {txn_date or 'N/A'}",
    ]

    if customer_memo:
        lines.extend(["", "## Memo", customer_memo])

    if include_private_notes and private_note:
        lines.extend(["", "## Private Notes", private_note])

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
    id_prefix: str | None = None,
    client: Any = None,
    resource_name: str | None = None,
    include_private_notes: bool = False,
    full_refresh: bool = False,
    allow_empty_sweep: bool = False,
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
        id_prefix: Namespace prefix for document IDs (default: "quickbooks:<realm_id>").
        client: Pre-configured QuickBooksClient instance (useful for hermetic unit testing).
        resource_name: Unique scope name for multi-account isolation (default: derived from realm).
        include_private_notes: Whether to ingest internal private notes (default: False).
        full_refresh: If True, bypasses watermark cursor and runs full sync & sweep reconciliation.
        allow_empty_sweep: If True, permits empty API responses to tombstone existing records.

    Returns:
        dlt source tagged with document mode for Cognee entity extraction.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    # Validate entities allowlist deterministically
    if entities is None:
        sync_entities = list(SUPPORTED_ENTITIES)
    else:
        if not entities:
            raise ValueError(
                f"entities must not be empty. Valid options: {list(SUPPORTED_ENTITIES)}."
            )
        unknown = [e for e in entities if e not in SUPPORTED_ENTITIES]
        if unknown:
            raise ValueError(
                f"Invalid entity/entities in selection: {unknown}. "
                f"Valid options: {list(SUPPORTED_ENTITIES)}."
            )
        sync_entities = list(dict.fromkeys(entities))

    active_client = client or QuickBooksClient(
        realm_id=realm_id,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        access_token=access_token,
        sandbox=sandbox,
    )

    resolved_realm = getattr(active_client, "realm_id", None) or realm_id or "default"
    effective_prefix = id_prefix or f"{DEFAULT_ID_PREFIX}:{resolved_realm}"
    scope_name = resource_name or f"quickbooks_{resolved_realm}"

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
            last_updated = None if full_refresh else state.get("last_updated_time")
            known_ids = set(state.get("known_ids", []))

            current_sync_ids: set[str] = set()
            newest_updated = last_updated
            records_fetched = 0

            try:
                for item in active_client.fetch_all(entity_name, last_updated_time=last_updated):
                    card = to_card_fn(
                        item,
                        id_prefix=effective_prefix,
                        include_private_notes=include_private_notes,
                    )
                    raw_id = str(item.get("Id", ""))
                    current_sync_ids.add(raw_id)
                    records_fetched += 1

                    meta = item.get("MetaData") or {}
                    last_mod = meta.get("LastUpdatedTime")
                    if last_mod and (newest_updated is None or last_mod > newest_updated):
                        newest_updated = last_mod

                    yield card

                # ── Deletion reconciliation ─────────────────────────────────
                # Two-tier reconciliation:
                # 1. Primary: Attempt CDC query when cursor is present
                cdc_succeeded = False
                cdc_deleted_ids: set[str] = set()

                if last_updated:
                    try:
                        cdc_records = active_client.fetch_cdc(
                            [entity_name], changed_since=last_updated
                        )
                        for cdc_item in cdc_records:
                            if cdc_item.get("status") == "Deleted":
                                del_id = str(cdc_item.get("Id", ""))
                                cdc_deleted_ids.add(del_id)
                                yield {
                                    "id": f"{effective_prefix}:{entity_name.lower()}:{del_id}",
                                    "_deleted": True,
                                }
                        known_ids.difference_update(cdc_deleted_ids)
                        cdc_succeeded = True
                    except Exception as cdc_exc:
                        logger.warning(
                            "QuickBooks CDC query failed, falling back to active ID sweep: %s",
                            cdc_exc,
                        )
                        cdc_succeeded = False

                # 2. Fallback sweep: If no cursor (full backfill/refresh) OR CDC failed/unreliable
                if not last_updated or not cdc_succeeded:
                    # Query confirmed active IDs for this entity
                    if hasattr(active_client, "fetch_active_ids"):
                        active_ids = active_client.fetch_active_ids(entity_name)
                    else:
                        active_ids = current_sync_ids

                    # Anti-corruption safeguard: abort if 0 records returned while known
                    if len(active_ids) == 0 and known_ids and not allow_empty_sweep:
                        logger.warning(
                            "QuickBooks %s sweep returned 0 records but %d were known. "
                            "Aborting deletion reconciliation to prevent accidental graph wipe.",
                            entity_name,
                            len(known_ids),
                        )
                        return

                    deleted_ids = known_ids - active_ids
                    for d_id in sorted(deleted_ids):
                        yield {
                            "id": f"{effective_prefix}:{entity_name.lower()}:{d_id}",
                            "_deleted": True,
                        }
                    state["known_ids"] = sorted(active_ids)
                else:
                    # Incremental CDC path succeeded: accumulate newly seen IDs
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
    # Scope pipeline state per realm / resource_name
    setattr(source, PIPELINE_SCOPE_ATTR, scope_name)
    return source
