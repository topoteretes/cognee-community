"""DLT source for Splitwise expenses (incremental sync, merge + tombstone).

Fetches Splitwise expenses, groups, and balances, yielding them as dlt resources
for cognee's document-mode ingestion pipeline.

Expenses are ingested as *normal documents*: the source declares
``cognee_document_source = "splitwise"``, so ``resolve_dlt_sources`` tags each row
``external_metadata["source"] = "splitwise"``. Each expense flows through the
standard cognify entity-extraction pipeline.

Sync strategy:
- ``write_disposition="merge"`` with ``primary_key="id"`` (Gmail-style).
- Voided expenses (``deleted_at`` present) emit ``_deleted=True`` hard-delete
  tombstones, causing dlt to purge them on merge.
- Incremental sync via ``updated_at`` high-watermark in dlt ``resource_state``.
- Content-hash watermark on the expense prose skips LLM re-processing on re-sync.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import dlt
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

from cognee_community_connector_splitwise.auth import build_splitwise_client

logger = get_logger("splitwise_connector")

SPLITWISE_TABLE_NAME = "splitwise_expenses"
SPLITWISE_SOURCE_NAME = "splitwise"

_MAX_RETRIES = 3
_MAX_EXPENSES = 1000
_PAGE_SIZE = 20


# ---------------------------------------------------------------------------
# Parsing and hashing utilities
# ---------------------------------------------------------------------------


def _epoch_to_iso(ts: int | float | None) -> str:
    """Convert epoch seconds to ISO 8601 UTC string."""
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts).isoformat() + "Z"


def _compute_content_hash(description: str, cost: str, shares: str) -> str:
    """Compute sha256 over the fields that drive LLM ingestion."""
    content = f"{description or ''}|{cost or ''}|{shares or ''}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _json_compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))


# ---------------------------------------------------------------------------
# API request helpers with retry & backoff
# ---------------------------------------------------------------------------


def _is_transient_error(exc: Exception) -> bool:
    """Check if an error from Splitwise API is transient and retryable."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status in (429, 500, 502, 503, 504)


def _execute_with_retry(request: Any, max_retries: int = _MAX_RETRIES) -> dict:
    """Execute an httpx request with exponential backoff on transient errors."""
    for attempt in range(max_retries):
        try:
            resp = request()
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            if attempt == max_retries - 1 or not _is_transient_error(exc):
                raise
            delay = 2**attempt
            logger.warning(
                "Splitwise API error: %s — retrying in %.1fs (%d/%d)",
                exc,
                delay,
                attempt + 1,
                max_retries,
            )
            import time

            time.sleep(delay)

    raise RuntimeError("Retry budget exhausted")


# ---------------------------------------------------------------------------
# API request helpers
# ---------------------------------------------------------------------------


def _fetch_expenses_page(
    client: Any,
    limit: int = _PAGE_SIZE,
    offset: int = 0,
    group_id: int | None = None,
    updated_after: str | None = None,
) -> list[dict]:
    """Fetch a single page of expenses from Splitwise API."""
    params = {"limit": min(limit, 100), "offset": offset}
    if group_id is not None:
        params["group_id"] = group_id
    if updated_after is not None:
        params["updated_after"] = updated_after

    def req():
        return client.get("/get_expenses", params=params)

    data = _execute_with_retry(req)
    return data.get("expenses", [])


def _fetch_groups(client: Any) -> list[dict]:
    """Fetch all groups."""

    def req():
        return client.get("/get_groups")

    data = _execute_with_retry(req)
    return data.get("groups", [])


def _fetch_current_user(client: Any) -> dict:
    """Fetch current user info."""

    def req():
        return client.get("/get_current_user")

    data = _execute_with_retry(req)
    return data.get("user", {})


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _build_expense_row(
    expense: dict,
    groups_by_id: dict[int, dict],
    current_user_id: int,
    content_hash: str,
    _deleted: bool = False,
) -> dict[str, Any]:
    """Build a document row matching cognee document expectations."""
    expense_id = str(expense.get("id", ""))
    description = expense.get("description", "")
    cost = str(expense.get("cost", ""))
    currency = expense.get("currency_code", "")
    date_str = expense.get("date", "")
    updated_at = expense.get("updated_at", "")
    expense.get("deleted_at")
    group_id = expense.get("group_id")
    created_by = expense.get("created_by", {})
    users = expense.get("users", [])

    group_name = groups_by_id.get(group_id, {}).get("name", "") if group_id else ""
    paid_by_name = created_by.get("first_name", "Unknown")

    shares_parts = []
    for u in users:
        uid = u.get("id")
        paid = u.get("paid_share", "0")
        owed = u.get("owed_share", "0")
        name = u.get("first_name", f"User {uid}")
        if float(paid) > 0 or float(owed) > 0:
            shares_parts.append(f"{name} paid {paid} / owed {owed}")
    shares_summary = " | ".join(shares_parts) if shares_parts else ""

    content_parts = [
        f"# [SPLITWISE] {description} — {cost} {currency}",
        f"- **Date**: {date_str} | **Group**: {group_name} | **Paid by**: {paid_by_name}",
    ]
    if shares_summary:
        content_parts.append(f"- **Split**: {shares_summary}")

    if expense.get("details"):
        content_parts.append(f"\n## Note\n{expense['details']}")

    receipt_url = expense.get("receipt", {}).get("original") if expense.get("receipt") else None
    if receipt_url:
        content_parts.append(f"- **Receipt**: {receipt_url}")

    content = "\n".join(content_parts)

    return {
        "id": expense_id,
        "url": f"https://secure.splitwise.com/expenses/{expense_id}",
        "title": f"{description} — {cost} {currency}"[:500],
        "content": content,
        "description": description,
        "cost": cost,
        "currency": currency,
        "date": date_str,
        "updated_at": updated_at,
        "group_id": str(group_id) if group_id else "",
        "group_name": group_name,
        "paid_by": paid_by_name,
        "shares_json": _json_compact(users),
        "receipt_url": receipt_url or "",
        "content_hash": content_hash,
        "_deleted": _deleted,
    }


def _deleted_row(expense_id: str) -> dict[str, Any]:
    """Emit a hard-delete row instructing dlt to purge the expense."""
    return {
        "id": expense_id,
        "url": f"https://secure.splitwise.com/expenses/{expense_id}",
        "title": "",
        "content": "",
        "description": "",
        "cost": "",
        "currency": "",
        "date": "",
        "updated_at": "",
        "group_id": "",
        "group_name": "",
        "paid_by": "",
        "shares_json": "{}",
        "receipt_url": "",
        "content_hash": "",
        "_deleted": True,
    }


# ---------------------------------------------------------------------------
# Expenses resource (module-level for direct testing)
# ---------------------------------------------------------------------------


def _create_expenses_resource(
    service: Any,
    groups_by_id: dict[int, dict],
    current_user_id: int,
    max_expenses: int,
    group_ids: list[int] | None,
):
    """Create the expenses dlt resource with all dependencies injected."""

    @dlt.resource(
        name=SPLITWISE_TABLE_NAME,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def splitwise_expenses():
        resource_state = dlt.current.resource_state()
        resource_state.setdefault("watermarks", {})
        last_updated_at = resource_state.get("last_updated_at")

        effective_updated_after = None if last_updated_at is None else last_updated_at

        fetched_count = 0
        max_updated_at_this_run = None

        offset = 0
        while fetched_count < max_expenses:
            page = _fetch_expenses_page(
                service,
                limit=_PAGE_SIZE,
                offset=offset,
                group_id=group_ids[0] if group_ids and len(group_ids) == 1 else None,
                updated_after=effective_updated_after,
            )
            if not page:
                break

            for expense in page:
                exp_id = str(expense.get("id", ""))
                updated_at = expense.get("updated_at", "")

                if updated_at and (
                    max_updated_at_this_run is None or updated_at > max_updated_at_this_run
                ):
                    max_updated_at_this_run = updated_at

                if expense.get("deleted_at"):
                    yield _deleted_row(exp_id)
                    continue

                description = expense.get("description", "")
                cost = str(expense.get("cost", ""))
                users = expense.get("users", [])
                shares_json = json.dumps(users, separators=(",", ":"))
                content_hash = _compute_content_hash(description, cost, shares_json)

                cached_wm = resource_state.setdefault("watermarks", {}).get(exp_id)
                if cached_wm and cached_wm.get("content_hash") == content_hash:
                    logger.debug("Expense %s unchanged, skipping", exp_id)
                    continue

                yield _build_expense_row(
                    expense, groups_by_id, current_user_id, content_hash, _deleted=False
                )
                resource_state.setdefault("watermarks", {})[exp_id] = {"content_hash": content_hash}
                fetched_count += 1

                if fetched_count >= max_expenses:
                    break

            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        if max_updated_at_this_run:
            resource_state["last_updated_at"] = max_updated_at_this_run

        logger.info("Splitwise connector: processed %d expenses.", fetched_count)

    return splitwise_expenses


# ---------------------------------------------------------------------------
# Public dlt source factory
# ---------------------------------------------------------------------------
def splitwise_source(
    api_key: str | None = None,
    base_url: str | None = None,
    group_ids: list[int] | None = None,
    include_groups: bool = True,
    include_balances: bool = True,
    max_expenses: int = _MAX_EXPENSES,
    client: Any = None,
):
    """Create a dlt source that yields Splitwise expenses as documents.

    Args:
        api_key: Splitwise personal access token. Falls back to ``SPLITWISE_API_KEY`` env var.
        base_url: Optional base URL override.
        group_ids: Optional list of group IDs to filter expenses.
        include_groups: Whether to yield group resources (default True).
        include_balances: Whether to yield balance resources (default True).
        max_expenses: Maximum number of expenses to fetch per run (default 1000).
        client: Pre-built Splitwise API client (test injection point).

    Returns:
        A dlt source yielding Splitwise expense documents for ``cognee.remember(...)``.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Splitwise connector requires the "splitwise" extra: '
            'pip install "cognee[splitwise]" (provides httpx).'
        ) from exc

    service = client or build_splitwise_client(api_key=api_key, base_url=base_url)

    # Pre-fetch groups for name lookup
    groups = _fetch_groups(service) if include_groups else []
    groups_by_id = {g["id"]: g for g in groups}

    # Pre-fetch current user
    current_user = _fetch_current_user(service)
    current_user_id = current_user.get("id", 0)

    # Create resources with injected dependencies
    _create_expenses_resource(service, groups_by_id, current_user_id, max_expenses, group_ids)

    @dlt.resource(name="splitwise_groups", primary_key="id", write_disposition="merge")
    def splitwise_groups():
        for group in groups:
            yield {
                "id": str(group["id"]),
                "name": group.get("name", ""),
                "type": group.get("type", ""),
                "members_count": len(group.get("members", [])),
                "balance": group.get("balance", []),
            }

    @dlt.resource(name="splitwise_balances", primary_key="user_id", write_disposition="merge")
    def splitwise_balances():
        for group in groups:
            for balance in group.get("balance", []):
                yield {
                    "user_id": str(balance.get("user_id", "")),
                    "group_id": str(group["id"]),
                    "group_name": group.get("name", ""),
                    "balance": str(balance.get("balance", "")),
                    "currency": balance.get("currency_code", ""),
                }

    @dlt.source(name=SPLITWISE_SOURCE_NAME)
    def _splitwise():
        resources = [
            _create_expenses_resource(
                service, groups_by_id, current_user_id, max_expenses, group_ids
            )
        ]
        if include_groups:
            resources.append(splitwise_groups)
        if include_balances:
            resources.append(splitwise_balances)
        return resources

    source = _splitwise()
    setattr(source, DOCUMENT_SOURCE_ATTR, SPLITWISE_SOURCE_NAME)
    return source


# Export for direct testing
def splitwise_expenses_resource(
    api_key: str | None = None,
    base_url: str | None = None,
    group_ids: list[int] | None = None,
    max_expenses: int = _MAX_EXPENSES,
    client: Any = None,
):
    """Standalone expenses source for direct testing (single resource, like youtube_source)."""
    service = client or build_splitwise_client(api_key=api_key, base_url=base_url)
    groups = _fetch_groups(service)
    groups_by_id = {g["id"]: g for g in groups}
    current_user = _fetch_current_user(service)
    current_user_id = current_user.get("id", 0)

    @dlt.source(name=SPLITWISE_SOURCE_NAME)
    def _splitwise():
        return _create_expenses_resource(
            service, groups_by_id, current_user_id, max_expenses, group_ids
        )

    source = _splitwise()
    setattr(source, DOCUMENT_SOURCE_ATTR, SPLITWISE_SOURCE_NAME)
    return source
