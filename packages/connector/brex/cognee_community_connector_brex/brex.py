"""DLT source for Brex corporate expenses, user memos, and budgets.

Supports:
1. brex_memos: Document mode (DOCUMENT_SOURCE_ATTR = "brex"). Formats corporate expenses,
   receipt annotations, and user justification memos into rich semantic document cards
   for LLM entity extraction and knowledge graph construction. Use `brex_memos_source()`.
2. brex_expenses: Structured relational mode. Emits normalized transaction rows with stable
   namespaced IDs, foreign keys, and incremental sync via `updated_at_start`.
3. brex_budgets: Structured relational mode. Ingests budget definitions and hierarchies
   from Brex Budgets API (v2).

Synchronization & Deletion Reconciliation Model:
- Incremental Runs (default): Scans modified records using Brex `updated_at_start` cursor.
  Captures transaction edits, refunds, receipts, and status transitions, accumulating newly
  seen IDs into `known_ids`. Deletion reconciliation does NOT occur during incremental runs
  to avoid false tombstones on delta sets.
- Full Refresh Runs (`full_refresh=True`): Bypasses watermark cursor, sweeps all active
  upstream records, and reconciles against accumulated `known_ids`. Missing or voided IDs
  emit tombstone rows (`{"_deleted": True}`) that trigger orphan cleanup across Cognee's
  relational, vector, and graph stores.
- Scheduled Recommendation: Run incremental syncs regularly (e.g. hourly) and schedule a
  periodic full refresh (e.g. daily or weekly) to reconcile deletions and archive old records.

Anti-Corruption & Empty-Sweep Guardrails:
- If any API request, pagination step, or network call fails during reconciliation, deletion
  tombstone emission is aborted immediately to protect graph state from transient failures.
- If a full sweep returns 0 records when previous records were known, deletion tombstones
  are skipped to prevent accidental corpus erasure, unless `allow_empty_sweep=True`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger

    logger = get_logger("brex_connector")
except ImportError:
    import logging

    logger = logging.getLogger("brex_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR, PIPELINE_SCOPE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"
    PIPELINE_SCOPE_ATTR = "cognee_pipeline_scope"

from .client import DEFAULT_BASE_URL, BrexClient

DEFAULT_ID_PREFIX = "brex"
BREX_EXPENSES_TABLE = "brex_expenses"
BREX_MEMOS_TABLE = "brex_memos"
BREX_BUDGETS_TABLE = "brex_budgets"
BREX_SOURCE_NAME = "brex"
BREX_MEMOS_SOURCE_NAME = "brex_memos"
VALID_RESOURCES = {"expenses", "memos", "budgets"}

_EXTRA_HINT = (
    'The Brex connector requires "dlt" and "requests": '
    'pip install "cognee-community-connector-brex"'
)


def _format_currency(amount: float | None, currency: str | None) -> str:
    """Format an amount and currency string for human-readable display."""
    curr = currency or "USD"
    if amount is None:
        return f"N/A {curr}"
    if amount < 0:
        return f"-${abs(amount):,.2f} {curr}"
    return f"${amount:,.2f} {curr}"


def _normalize_amount(raw_amount: Any) -> tuple[float | None, str | None]:
    """Extract numeric amount and currency from Brex amount object.

    Brex API returns amount in minor currency units (cents for USD) or standard decimal.
    """
    if isinstance(raw_amount, dict):
        amt = raw_amount.get("amount")
        curr = raw_amount.get("currency", "USD")
        if amt is not None:
            try:
                # Convert cents to dollars (including negative amounts/refunds)
                val = float(amt)
                if isinstance(amt, int):
                    val = amt / 100.0
                return val, curr
            except (ValueError, TypeError):
                return None, curr
        return None, curr
    elif isinstance(raw_amount, (int, float)):
        return float(raw_amount), "USD"
    return None, None


def _expense_to_row(
    expense: dict[str, Any],
    id_prefix: str = DEFAULT_ID_PREFIX,
) -> dict[str, Any]:
    """Convert raw Brex expense into structured relational row."""
    exp_id = str(expense["id"])
    amount_val, currency_val = _normalize_amount(expense.get("amount"))

    merchant_obj = expense.get("merchant") or {}
    merchant_name = (
        merchant_obj.get("name")
        or merchant_obj.get("raw_descriptor")
        or expense.get("merchant_name")
        or "Unknown Merchant"
    )

    user_obj = expense.get("user") or {}
    first_name = user_obj.get("first_name", "")
    last_name = user_obj.get("last_name", "")
    user_name = f"{first_name} {last_name}".strip() or expense.get("user_name") or ""

    dept_obj = expense.get("department") or {}
    budget_id = expense.get("budget_id")

    return {
        "id": f"{id_prefix}:expense:{exp_id}",
        "expense_id": exp_id,
        "amount": amount_val,
        "currency": currency_val or "USD",
        "merchant_name": merchant_name,
        "category": expense.get("category"),
        "memo": expense.get("memo") or "",
        "status": expense.get("status") or "APPROVED",
        "user_id": user_obj.get("id"),
        "user_name": user_name,
        "budget_id": f"{id_prefix}:budget:{budget_id}" if budget_id else None,
        "department_id": dept_obj.get("id"),
        "department_name": dept_obj.get("name"),
        "posted_at": expense.get("payment_posted_at") or expense.get("posted_at"),
        "purchased_at": expense.get("purchased_at"),
        "updated_at": expense.get("updated_at"),
        "_deleted": False,
    }


def _expense_to_memo_card(
    expense: dict[str, Any],
    id_prefix: str = DEFAULT_ID_PREFIX,
) -> dict[str, Any]:
    """Convert Brex expense and memo into a Markdown document card for Cognee LLM extraction."""
    exp_id = str(expense["id"])
    amount_val, currency_val = _normalize_amount(expense.get("amount"))
    formatted_amount = _format_currency(amount_val, currency_val)

    merchant_obj = expense.get("merchant") or {}
    merchant_name = (
        merchant_obj.get("name")
        or merchant_obj.get("raw_descriptor")
        or expense.get("merchant_name")
        or "Unknown Merchant"
    )

    user_obj = expense.get("user") or {}
    first_name = user_obj.get("first_name", "")
    last_name = user_obj.get("last_name", "")
    cardholder = f"{first_name} {last_name}".strip() or "Unassigned"

    dept_obj = expense.get("department") or {}
    dept_name = dept_obj.get("name") or "General"

    category = expense.get("category") or "Uncategorized"
    status = expense.get("status") or "APPROVED"
    posted_date = (
        expense.get("updated_at")
        or expense.get("payment_posted_at")
        or expense.get("posted_at")
        or expense.get("purchased_at")
        or "Unknown Date"
    )
    memo_text = expense.get("memo") or "No business justification memo provided."
    budget_id = expense.get("budget_id")

    lines = [
        f"# Expense: {merchant_name} ({formatted_amount})",
        f"**Cardholder:** {cardholder} | **Department:** {dept_name}",
        f"**Merchant:** {merchant_name} | **Category:** {category}",
        f"**Date:** {posted_date} | **Status:** {status}",
        "",
        "## Memo & Business Purpose",
        memo_text,
    ]

    if budget_id:
        lines.extend(
            [
                "",
                "## Budget Allocation",
                f"- **Budget ID:** `{id_prefix}:budget:{budget_id}`",
            ]
        )

    card_content = "\n".join(lines)

    return {
        "id": f"{id_prefix}:memo:{exp_id}",
        "title": f"Brex Memo: {merchant_name} ({formatted_amount})",
        "url": f"https://dashboard.brex.com/expenses/{exp_id}",
        "content": card_content,
        "_deleted": False,
    }


def _budget_to_row(
    budget: dict[str, Any],
    id_prefix: str = DEFAULT_ID_PREFIX,
) -> dict[str, Any]:
    """Convert raw Brex budget record (v2) into structured relational row."""
    raw_b_id = budget.get("budget_id") or budget.get("id")
    if not raw_b_id:
        raise KeyError("Brex budget record missing 'budget_id' or 'id'")
    budget_id = str(raw_b_id)

    # Brex v2 returns configured amount in minor currency units under 'amount'
    amount_val, amount_curr = _normalize_amount(budget.get("amount") or budget.get("limit"))
    # Brex v2 budget definitions do not return current spent amount
    spent_val, spent_curr = _normalize_amount(budget.get("spent"))
    parent_id = budget.get("parent_budget_id")
    recurrence = budget.get("period_recurrence_type") or budget.get("period_type")

    return {
        "id": f"{id_prefix}:budget:{budget_id}",
        "budget_id": budget_id,
        "name": budget.get("name") or "Unnamed Budget",
        "description": budget.get("description") or "",
        "budget_type": budget.get("budget_type"),
        "period_type": recurrence,
        "limit_amount": amount_val,
        "spent_amount": spent_val,
        "currency": amount_curr or spent_curr or "USD",
        "parent_budget_id": f"{id_prefix}:budget:{parent_id}" if parent_id else None,
        "start_date": budget.get("start_date"),
        "end_date": budget.get("end_date"),
        "status": budget.get("status") or "ACTIVE",
        "_deleted": False,
    }


def brex_source(
    token: str | None = None,
    *,
    include: list[str] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    id_prefix: str = DEFAULT_ID_PREFIX,
    account_id: str | None = None,
    client: Any = None,
    full_refresh: bool = False,
    allow_empty_sweep: bool = False,
    resource_name: str | None = None,
):
    """Return a dlt source yielding Brex expenses, memos, and budgets.

    Args:
        token: Brex API User or Admin token. Falls back to BREX_API_TOKEN environment variable.
        include: List of resources to sync (defaults to ["expenses", "budgets"]).
                 Valid values: "expenses", "memos", "budgets".
        base_url: Brex API base URL (default: 'https://api.brex.com').
        id_prefix: Namespace prefix for global IDs (default: 'brex').
        account_id: Optional account ID to scope global IDs and DLT state for multi-account.
        client: Pre-built BrexClient instance (for offline testing injection).
        full_refresh: If True, bypasses watermark cursor and runs a full sweep with deletion
                     reconciliation. Recommended periodically (e.g. daily/weekly).
        allow_empty_sweep: If True, permits empty API responses to tombstone existing records.
        resource_name: Optional pipeline scope identifier for multi-account / isolated state.

    Returns:
        A dlt source configured with primary_key="id", write_disposition="merge",
        and tombstone deletion handling.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

    if include is not None:
        if not include:
            raise ValueError(
                "include must not be empty. Valid options: 'expenses', 'memos', 'budgets'."
            )
        invalid = set(include) - VALID_RESOURCES
        if invalid:
            raise ValueError(
                f"Invalid resource(s) in include: {sorted(invalid)}. "
                f"Valid options: {sorted(VALID_RESOURCES)}."
            )
        resources_to_sync = set(include)
    else:
        resources_to_sync = {"expenses", "budgets"}

    resolved_token = token or os.environ.get("BREX_API_TOKEN")
    if client is None:
        if not resolved_token:
            raise ValueError(
                "Brex API token is required: pass token= or set the BREX_API_TOKEN "
                "environment variable."
            )
        active_client = BrexClient(token=resolved_token, base_url=base_url)
    else:
        active_client = client

    effective_prefix = f"{id_prefix}:{account_id}" if account_id else id_prefix

    @dlt.resource(
        name=BREX_EXPENSES_TABLE,
        primary_key="id",
        write_disposition="merge",
        columns={
            "id": {"data_type": "text"},
            "expense_id": {"data_type": "text"},
            "amount": {"data_type": "double"},
            "currency": {"data_type": "text"},
            "merchant_name": {"data_type": "text"},
            "category": {"data_type": "text"},
            "memo": {"data_type": "text"},
            "status": {"data_type": "text"},
            "user_id": {"data_type": "text"},
            "user_name": {"data_type": "text"},
            "budget_id": {"data_type": "text"},
            "department_id": {"data_type": "text"},
            "department_name": {"data_type": "text"},
            "posted_at": {"data_type": "text"},
            "purchased_at": {"data_type": "text"},
            "updated_at": {"data_type": "text"},
            "_deleted": {"data_type": "bool", "hard_delete": True},
        },
    )
    def brex_expenses() -> Iterator[dict[str, Any]]:
        """Yield structured rows for card expenses with incremental sync and reconciliation."""
        state = dlt.current.resource_state()
        last_updated = (
            None if full_refresh else (state.get("last_updated_at") or state.get("last_posted_at"))
        )
        known_ids = set(state.get("known_ids", []))

        current_sync_keys = set()
        newest_updated = last_updated

        try:
            for exp in active_client.fetch_all_expenses(updated_at_start=last_updated):
                row = _expense_to_row(exp, id_prefix=effective_prefix)
                exp_id = str(exp["id"])
                current_sync_keys.add(exp_id)

                updated = (
                    exp.get("updated_at") or exp.get("payment_posted_at") or exp.get("posted_at")
                )
                if updated and (newest_updated is None or updated > newest_updated):
                    newest_updated = updated

                yield row

            # Full sweep reconciliation of deleted records
            if not last_updated:
                if known_ids and not current_sync_keys and not allow_empty_sweep:
                    logger.warning(
                        "Brex: expense sweep returned 0 records but %d were known; "
                        "skipping deletion to prevent mass tombstoning.",
                        len(known_ids),
                    )
                    return

                deleted_ids = known_ids - current_sync_keys
                for d_id in sorted(deleted_ids):
                    yield {
                        "id": f"{effective_prefix}:expense:{d_id}",
                        "_deleted": True,
                    }
                state["known_ids"] = sorted(current_sync_keys)
            else:
                # Incremental delta run: add newly seen records to known_ids
                state["known_ids"] = sorted(known_ids | current_sync_keys)

            if newest_updated:
                state["last_updated_at"] = newest_updated
                state["last_posted_at"] = newest_updated

        except Exception as exc:
            logger.error("Brex expenses sync failed: %s. Aborting reconciliation.", exc)
            raise

    @dlt.resource(
        name=BREX_MEMOS_TABLE,
        primary_key="id",
        write_disposition="merge",
        columns={
            "id": {"data_type": "text"},
            "title": {"data_type": "text"},
            "url": {"data_type": "text"},
            "content": {"data_type": "text"},
            "_deleted": {"data_type": "bool", "hard_delete": True},
        },
    )
    def brex_memos() -> Iterator[dict[str, Any]]:
        """Yield Markdown document cards for expenses with memos (Document Mode)."""
        state = dlt.current.resource_state()
        last_updated = (
            None if full_refresh else (state.get("last_updated_at") or state.get("last_posted_at"))
        )
        known_memo_ids = set(state.get("known_ids", []))

        current_sync_keys = set()
        newest_updated = last_updated

        try:
            for exp in active_client.fetch_all_expenses(updated_at_start=last_updated):
                card = _expense_to_memo_card(exp, id_prefix=effective_prefix)
                exp_id = str(exp["id"])
                current_sync_keys.add(exp_id)

                updated = (
                    exp.get("updated_at") or exp.get("payment_posted_at") or exp.get("posted_at")
                )
                if updated and (newest_updated is None or updated > newest_updated):
                    newest_updated = updated

                yield card

            # Full sweep reconciliation of deleted memos
            if not last_updated:
                if known_memo_ids and not current_sync_keys and not allow_empty_sweep:
                    logger.warning(
                        "Brex: memo sweep returned 0 records but %d were known; "
                        "skipping deletion to prevent mass tombstoning.",
                        len(known_memo_ids),
                    )
                    return

                deleted_ids = known_memo_ids - current_sync_keys
                for d_id in sorted(deleted_ids):
                    yield {
                        "id": f"{effective_prefix}:memo:{d_id}",
                        "_deleted": True,
                    }
                state["known_ids"] = sorted(current_sync_keys)
            else:
                # Incremental delta run: add newly seen memo records to known_ids
                state["known_ids"] = sorted(known_memo_ids | current_sync_keys)

            if newest_updated:
                state["last_updated_at"] = newest_updated
                state["last_posted_at"] = newest_updated

        except Exception as exc:
            logger.error("Brex memos sync failed: %s. Aborting reconciliation.", exc)
            raise

    @dlt.resource(
        name=BREX_BUDGETS_TABLE,
        primary_key="id",
        write_disposition="merge",
        columns={
            "id": {"data_type": "text"},
            "budget_id": {"data_type": "text"},
            "name": {"data_type": "text"},
            "description": {"data_type": "text"},
            "budget_type": {"data_type": "text"},
            "period_type": {"data_type": "text"},
            "limit_amount": {"data_type": "double"},
            "spent_amount": {"data_type": "double"},
            "currency": {"data_type": "text"},
            "parent_budget_id": {"data_type": "text"},
            "start_date": {"data_type": "text"},
            "end_date": {"data_type": "text"},
            "status": {"data_type": "text"},
            "_deleted": {"data_type": "bool", "hard_delete": True},
        },
    )
    def brex_budgets() -> Iterator[dict[str, Any]]:
        """Yield structured rows for departmental budgets with snapshot deletion reconciliation."""
        state = dlt.current.resource_state()
        known_budget_ids = set(state.get("known_ids", []))
        current_sync_keys = set()

        try:
            for budget in active_client.fetch_all_budgets():
                row = _budget_to_row(budget, id_prefix=effective_prefix)
                raw_b_id = budget.get("budget_id") or budget.get("id")
                b_id = str(raw_b_id) if raw_b_id else ""
                if b_id:
                    current_sync_keys.add(b_id)
                yield row

            # Empty sweep protection
            if known_budget_ids and not current_sync_keys and not allow_empty_sweep:
                logger.warning(
                    "Brex: budget sweep returned 0 records but %d were known; "
                    "skipping deletion to prevent mass tombstoning.",
                    len(known_budget_ids),
                )
                return

            # Emit tombstones for deleted budgets
            deleted_ids = known_budget_ids - current_sync_keys
            for d_id in sorted(deleted_ids):
                yield {
                    "id": f"{effective_prefix}:budget:{d_id}",
                    "_deleted": True,
                }

            state["known_ids"] = sorted(current_sync_keys)

        except Exception as exc:
            logger.error("Brex budgets sync failed: %s. Aborting reconciliation.", exc)
            raise

    @dlt.source(name=BREX_SOURCE_NAME)
    def _brex():
        active = []
        if "expenses" in resources_to_sync:
            active.append(brex_expenses)
        if "memos" in resources_to_sync:
            active.append(brex_memos)
        if "budgets" in resources_to_sync:
            active.append(brex_budgets)
        return active

    source = _brex()

    # Isolate dlt pipeline state if resource_name or account_id is provided
    scope_name = resource_name or (f"brex_{account_id}" if account_id else None)
    if scope_name:
        setattr(source, PIPELINE_SCOPE_ATTR, scope_name)

    # Route brex_memos specifically through document mode
    if "memos" in resources_to_sync:
        setattr(brex_memos, DOCUMENT_SOURCE_ATTR, "brex")
        if BREX_MEMOS_TABLE in source.resources:
            setattr(source.resources[BREX_MEMOS_TABLE], DOCUMENT_SOURCE_ATTR, "brex")
        # Tag source as document mode ONLY when selection is strictly memos
        if resources_to_sync == {"memos"}:
            setattr(source, DOCUMENT_SOURCE_ATTR, "brex")

    return source


def brex_memos_source(
    token: str | None = None,
    *,
    base_url: str = DEFAULT_BASE_URL,
    id_prefix: str = DEFAULT_ID_PREFIX,
    account_id: str | None = None,
    client: Any = None,
    full_refresh: bool = False,
    allow_empty_sweep: bool = False,
    resource_name: str | None = None,
):
    """Return a dedicated document-mode dlt source yielding Brex memos.

    Tagged with DOCUMENT_SOURCE_ATTR = 'brex' for routing to Cognee LLM knowledge graph extraction.
    """
    return brex_source(
        token=token,
        include=["memos"],
        base_url=base_url,
        id_prefix=id_prefix,
        account_id=account_id,
        client=client,
        full_refresh=full_refresh,
        allow_empty_sweep=allow_empty_sweep,
        resource_name=resource_name,
    )
