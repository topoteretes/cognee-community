"""YNAB (You Need A Budget) data-source connector for Cognee.

Ingests budgets, transactions, categories, and account spending activity
from YNAB into Cognee memory for natural language personal finance intelligence.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Generator, List, Optional
import dlt
import httpx

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

logger = logging.getLogger(__name__)

YNAB_API_BASE_URL = "https://api.ynab.com/v1"


def _milliunits_to_currency(milliunits: Optional[int]) -> float:
    """Convert YNAB integer milliunits (e.g. 12340) to standard currency float (12.34)."""
    if milliunits is None:
        return 0.0
    return round(milliunits / 1000.0, 2)


class YNABClient:
    """Client for querying the YNAB REST API v1."""

    def __init__(
        self,
        access_token: str,
        base_url: str = YNAB_API_BASE_URL,
        http_client: Optional[Any] = None,
        timeout: float = 30.0,
    ) -> None:
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self._http_client = http_client or httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "CogneeCommunityYNABConnector/0.1.0",
                "Accept": "application/json",
            },
        )

    def get_budgets(self) -> List[Dict[str, Any]]:
        """Retrieve all budgets accessible by the user."""
        url = f"{self.base_url}/budgets"
        try:
            resp = self._http_client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("budgets", [])
        except Exception as exc:
            logger.error("Failed to fetch YNAB budgets: %s", exc)
            raise

    def get_transactions(
        self,
        budget_id: str,
        since_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve transactions for a specific budget."""
        url = f"{self.base_url}/budgets/{budget_id}/transactions"
        params = {}
        if since_date:
            params["since_date"] = since_date

        try:
            resp = self._http_client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("transactions", [])
        except Exception as exc:
            logger.error("Failed to fetch YNAB transactions for budget %s: %s", budget_id, exc)
            raise

    def get_categories(self, budget_id: str) -> List[Dict[str, Any]]:
        """Retrieve category groups and categories for a budget."""
        url = f"{self.base_url}/budgets/{budget_id}/categories"
        try:
            resp = self._http_client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("category_groups", [])
        except Exception as exc:
            logger.error("Failed to fetch YNAB categories for budget %s: %s", budget_id, exc)
            raise


def _transaction_to_document(tx: Dict[str, Any], budget_id: str) -> Optional[Dict[str, Any]]:
    """Convert a YNAB transaction into a structured Markdown document."""
    tx_id = tx.get("id")
    if not tx_id:
        return None

    date = tx.get("date", "Unknown date")
    amount = _milliunits_to_currency(tx.get("amount", 0))
    payee = tx.get("payee_name") or "Unknown Payee"
    category = tx.get("category_name") or "Uncategorized"
    account = tx.get("account_name") or "Primary Account"
    cleared = tx.get("cleared", "uncleared")
    approved = tx.get("approved", False)
    memo = tx.get("memo") or "No memo"

    status_str = "Cleared" if cleared == "cleared" else ("Reconciled" if cleared == "reconciled" else "Uncleared")
    approval_str = "Approved" if approved else "Needs Review"

    subtransactions = tx.get("subtransactions", [])
    splits_section = ""
    if subtransactions:
        split_lines = ["\n### Split Details:"]
        for sub in subtransactions:
            sub_amt = _milliunits_to_currency(sub.get("amount", 0))
            sub_cat = sub.get("category_name") or "Split category"
            sub_memo = sub.get("memo") or ""
            split_lines.append(f"- **{sub_cat}:** ${sub_amt:.2f} {f'({sub_memo})' if sub_memo else ''}")
        splits_section = "\n".join(split_lines)

    content = f"""# YNAB Transaction: {payee} (${amount:.2f})
**Transaction Date:** {date}
**Amount:** ${amount:.2f}
**Payee:** {payee}
**Category:** {category}
**Account:** {account}
**Status:** {status_str} | **Review:** {approval_str}
**Memo:** {memo}
{splits_section}

---
*Ingested from YNAB Budget `{budget_id}` via Cognee community connector.*"""

    return {
        "id": f"ynab_tx_{tx_id}",
        "url": f"https://app.ynab.com/{budget_id}/accounts",
        "title": f"Transaction: {payee} - ${amount:.2f} ({date})",
        "content": content,
    }


def _category_to_document(cat: Dict[str, Any], group_name: str, budget_id: str) -> Optional[Dict[str, Any]]:
    """Convert a YNAB budget category into a structured Markdown document."""
    cat_id = cat.get("id")
    if not cat_id or cat.get("deleted", False):
        return None

    name = cat.get("name") or "Unnamed Category"
    budgeted = _milliunits_to_currency(cat.get("budgeted", 0))
    activity = _milliunits_to_currency(cat.get("activity", 0))
    balance = _milliunits_to_currency(cat.get("balance", 0))
    note = cat.get("note") or "No notes"
    goal_type = cat.get("goal_type") or "None"

    content = f"""# YNAB Category: {name} (Group: {group_name})
**Category Group:** {group_name}
**Available Balance:** ${balance:.2f}
**Budgeted This Month:** ${budgeted:.2f}
**Activity / Spending:** ${activity:.2f}
**Target / Goal:** {goal_type}
**Notes:** {note}

---
*Ingested from YNAB Budget `{budget_id}` via Cognee community connector.*"""

    return {
        "id": f"ynab_cat_{cat_id}",
        "url": f"https://app.ynab.com/{budget_id}/budget",
        "title": f"Budget Category: {name} (${balance:.2f} available)",
        "content": content,
    }


@dlt.source(name="ynab_source")
def ynab_source(
    access_token: Optional[str] = None,
    budget_id: Optional[str] = None,
    since_date: Optional[str] = None,
    client: Optional[YNABClient] = None,
) -> Any:
    """DLT data-source for ingesting YNAB financial data into Cognee."""
    ynab_client = client or YNABClient(access_token=access_token or "")

    @dlt.resource(
        name="transactions",
        write_disposition="replace",
        primary_key="id",
    )
    def transactions() -> Generator[Dict[str, Any], None, None]:
        # Determine target budgets
        target_budget_ids = [budget_id] if budget_id else []
        if not target_budget_ids:
            budgets = ynab_client.get_budgets()
            target_budget_ids = [b["id"] for b in budgets if "id" in b]

        for b_id in target_budget_ids:
            tx_list = ynab_client.get_transactions(budget_id=b_id, since_date=since_date)
            for tx in tx_list:
                doc = _transaction_to_document(tx, budget_id=b_id)
                if doc:
                    yield doc

            cat_groups = ynab_client.get_categories(budget_id=b_id)
            for group in cat_groups:
                g_name = group.get("name", "General")
                for cat in group.get("categories", []):
                    cat_doc = _category_to_document(cat, group_name=g_name, budget_id=b_id)
                    if cat_doc:
                        yield cat_doc

    resource = transactions()
    setattr(resource, DOCUMENT_SOURCE_ATTR, "ynab")
    return resource
