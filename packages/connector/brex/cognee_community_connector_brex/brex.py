"""DLT source for Brex corporate expenses, user memos, and budgets.

Supports:
1. brex_memos: Document mode (DOCUMENT_SOURCE_ATTR = "brex"). Formats corporate expenses,
   receipt annotations, and user justification memos into rich semantic document cards
   for LLM entity extraction and knowledge graph construction.
2. brex_expenses: Structured relational mode. Emits normalized transaction rows with stable
   namespaced IDs, foreign keys, and incremental sync via `posted_at_start`.
3. brex_budgets: Structured relational mode. Ingests budget definitions and hierarchies.

Anti-corruption guardrail:
If any API request, pagination step, or network call fails during reconciliation, deletion
tombstone emission is aborted immediately to protect graph state from transient failures.
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
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

from .client import DEFAULT_BASE_URL, BrexClient

DEFAULT_ID_PREFIX = "brex"
BREX_EXPENSES_TABLE = "brex_expenses"
BREX_MEMOS_TABLE = "brex_memos"
BREX_BUDGETS_TABLE = "brex_budgets"
BREX_SOURCE_NAME = "brex"

_EXTRA_HINT = (
    'The Brex connector requires "dlt" and "requests": '
    'pip install "cognee-community-connector-brex"'
)


def _format_currency(amount: float | None, currency: str | None) -> str:
    """Format an amount and currency string for human-readable display."""
    curr = currency or "USD"
    if amount is None:
        return f"N/A {curr}"
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
                # Convert cents to dollars if integer representation
                val = float(amt)
                if isinstance(amt, int) and amt > 0:
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
        "posted_at": expense.get("posted_at"),
        "purchased_at": expense.get("purchased_at"),
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
    posted_date = expense.get("posted_at") or expense.get("purchased_at") or "Unknown Date"
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
    """Convert raw Brex budget record into structured relational row."""
    budget_id = str(budget["id"])
    limit_val, limit_curr = _normalize_amount(budget.get("limit"))
    spent_val, spent_curr = _normalize_amount(budget.get("spent"))
    parent_id = budget.get("parent_budget_id")

    return {
        "id": f"{id_prefix}:budget:{budget_id}",
        "budget_id": budget_id,
        "name": budget.get("name") or "Unnamed Budget",
        "description": budget.get("description") or "",
        "budget_type": budget.get("budget_type"),
        "period_type": budget.get("period_type"),
        "limit_amount": limit_val,
        "spent_amount": spent_val,
        "currency": limit_curr or spent_curr or "USD",
        "parent_budget_id": f"{id_prefix}:budget:{parent_id}" if parent_id else None,
        "start_date": budget.get("start_date"),
        "end_date": budget.get("end_date"),
        "_deleted": False,
    }


def brex_source(
    token: str | None = None,
    *,
    include: list[str] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    id_prefix: str = DEFAULT_ID_PREFIX,
    client: Any = None,
):
    """Return a dlt source yielding Brex expenses, memos, and budgets.

    Args:
        token: Brex API User or Admin token. Falls back to BREX_API_TOKEN environment variable.
        include: List of resources to sync (defaults to ["expenses", "memos", "budgets"]).
        base_url: Brex API base URL (default: 'https://platform.brexapis.com/v1').
        id_prefix: Namespace prefix for global IDs (default: 'brex').
        client: Pre-built BrexClient instance (for offline testing injection).

    Returns:
        A dlt source configured with primary_key="id", write_disposition="merge",
        and tombstone deletion handling.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(_EXTRA_HINT) from exc

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

    resources_to_sync = set(include or ["expenses", "memos", "budgets"])

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
            "_deleted": {"data_type": "bool", "hard_delete": True},
        },
    )
    def brex_expenses() -> Iterator[dict[str, Any]]:
        """Yield structured rows for card expenses with incremental sync and reconciliation."""
        state = dlt.current.resource_state()
        last_posted = state.get("last_posted_at")
        known_ids = set(state.get("known_ids", []))

        current_sync_keys = set()
        newest_posted = last_posted

        try:
            for exp in active_client.fetch_all_expenses(posted_at_start=last_posted):
                row = _expense_to_row(exp, id_prefix=id_prefix)
                exp_id = str(exp["id"])
                current_sync_keys.add(exp_id)

                posted = exp.get("posted_at")
                if posted and (newest_posted is None or posted > newest_posted):
                    newest_posted = posted

                yield row

            # Reconciliation of deleted records (only on full scan without cursor)
            if not last_posted:
                deleted_ids = known_ids - current_sync_keys
                for d_id in deleted_ids:
                    yield {
                        "id": f"{id_prefix}:expense:{d_id}",
                        "_deleted": True,
                    }
                state["known_ids"] = sorted(current_sync_keys)

            if newest_posted:
                state["last_posted_at"] = newest_posted

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
        last_posted = state.get("last_posted_at")
        known_memo_ids = set(state.get("known_ids", []))

        current_sync_keys = set()
        newest_posted = last_posted

        try:
            for exp in active_client.fetch_all_expenses(posted_at_start=last_posted):
                card = _expense_to_memo_card(exp, id_prefix=id_prefix)
                exp_id = str(exp["id"])
                current_sync_keys.add(exp_id)

                posted = exp.get("posted_at")
                if posted and (newest_posted is None or posted > newest_posted):
                    newest_posted = posted

                yield card

            # Reconciliation of deleted records on full scan
            if not last_posted:
                deleted_ids = known_memo_ids - current_sync_keys
                for d_id in deleted_ids:
                    yield {
                        "id": f"{id_prefix}:memo:{d_id}",
                        "_deleted": True,
                    }
                state["known_ids"] = sorted(current_sync_keys)

            if newest_posted:
                state["last_posted_at"] = newest_posted

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
                row = _budget_to_row(budget, id_prefix=id_prefix)
                b_id = str(budget["id"])
                current_sync_keys.add(b_id)
                yield row

            # Emit tombstones for deleted budgets
            deleted_ids = known_budget_ids - current_sync_keys
            for d_id in deleted_ids:
                yield {
                    "id": f"{id_prefix}:budget:{d_id}",
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

    # Route brex_memos specifically through document mode
    if "memos" in resources_to_sync:
        setattr(brex_memos, DOCUMENT_SOURCE_ATTR, "brex")
        if BREX_MEMOS_TABLE in source.resources:
            setattr(source.resources[BREX_MEMOS_TABLE], DOCUMENT_SOURCE_ATTR, "brex")
        setattr(source, DOCUMENT_SOURCE_ATTR, "brex")

    return source
