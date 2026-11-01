# Splitwise Connector — Proposed Solution (Beginner-Friendly, E)

## Problem Statement
cognee has five data-source connectors today (confluence, gmail, google-drive, notion, slack) against roughly 120 planned. Data sources are the thinnest surface in the project.
Shared-money knowledge ("who paid for Goa, who still owes for dinner, why did March balances spike?") lives in Splitwise and never reaches memory. Finance coverage is spend-side (Ramp #4796, Brex #4797) and bookkeeping (QuickBooks #4802, Xero #4803) — personal split expenses are missing. Plaid is the full answer but needs bank approval; Splitwise is the hackathon-sized slice.

## Proposed Solution
Add a `splitwise` connector under `packages/connector/splitwise/` in cognee-community, following the `gmail` merge + cursor pattern.

**Auth**: personal token / OAuth1 access token (`SPLITWISE_API_KEY` or `SPLITWISE_CONSUMER_KEY/SECRET + TOKEN/SECRET`). v1 documents token flow, tests use direct header injection.
**Ingest**: expenses (description, amount, payer, owers, shares, group, date, receipt_url) + groups + balances — one doc per expense
**Incremental sync**: `updated_at` / `id` high-watermark (`resource_state["last_expense_id"]` + `last_updated_at`)
**Forget-on-delete**: `write_disposition="merge"` + `_deleted` tombstone (API returns `deleted_at` for voided expenses)

### Watch out for
- OAuth1 (not OAuth2) — use `requests-oauthlib` or pre-minted token in README; don't hand-roll signature in v1.
- Amounts are user-private — prose limited to description + shares bands; exact payer email → columns only.
- `limit` pagination (not cursor) — walk `limit/offset` until empty page; cap `max_expenses=1000` default.

### Package layout to follow
```
packages/connector/splitwise/
├── README.md
├── cognee_community_connector_splitwise/
│   ├── __init__.py          # exports splitwise_source
│   ├── splitwise.py         # dlt source, limit/offset pagination, cursor
│   └── auth.py              # token header builder + OAuth1 notes
├── examples/
│   └── basic_usage.py
├── tests/
│   ├── test_ingestion.py
│   ├── test_sync.py
│   └── test_deletions.py
└── pyproject.toml
```

Read `packages/connector/gmail/` (merge + `_deleted`) + `ramp` proposal (zero-knowledge filter idea).

## Acceptance Criteria
1. User sets token + `group_ids` optional filter, selects expenses/groups/balances
2. Expenses ingested and searchable (`DOCUMENT_SOURCE_ATTR="splitwise"`)
3. Re-sync yields only new/changed expenses
4. Voided expense emits `_deleted` and purges on next sync
5. README + example + mocked tests

---

## Use Cases

1. **Trip settlement recall** — sync a `Goa Trip` group; ask "who paid for the Airbnb and how much does each person owe?" Document per expense (payer + shares folded) makes this a single-graph lookup instead of scrolling the app.
2. **Roommate ledger** — sync `Flat 4B` rent/groceries/bills; ask "what did we spend on groceries in September vs October, and who is net owed?" Balances + monthly day-folding answer without spreadsheets.
3. **Friend-debt nudge** — sync unsettled balances; ask "who still owes me over 2000 INR across all groups?" Structured `owed_share` columns drive the filter, prose gives the reason to include in the reminder.
4. **Dispute audit** — voided/edited expenses emit `_deleted` / re-yield with new hash; ask "which expenses were edited or voided last month and why?" for a tamper-evident trail.

## API Mapping (Splitwise)

| Resource | Endpoint | Key Fields | Pagination |
|----------|----------|------------|------------|
| Expenses | `GET /api/3.0/get_expenses?limit=&offset=` | `id, description, cost, currency_code, date, updated_at, deleted_at, created_by, repayments, users{id, paid_share, owed_share}` | `limit/offset` |
| Groups | `GET /api/3.0/get_groups` | `id, name, members, balance` | single page |
| User | `GET /api/3.0/get_current_user` | `id, first_name` | once |

## Key Design Decisions

### 1. Merge + tombstone (like Gmail)
- Resource `splitwise_expenses`, `primary_key="id"`, `write_disposition="merge"`.
- `deleted_at != null` → yield with `_deleted=True`. No replace needed.

### 2. Cursor: `updated_at` + id dedupe
- `resource_state["last_updated_at"]`; `limit=20` pages; per-expense `sha256(description+cost+shares)` skips unchanged (0 LLM calls).
- Lookback 7 days for late edits (same pattern as Ramp lookback, but tiny).

### 3. One Document Per Expense
```markdown
# [SPLITWISE] Goa Airbnb — 12,000 INR
- **Date**: 2026-09-12 | **Group**: Goa Trip | **Paid by**: Rahul
- **Split**: Rahul 4000 / Amit 4000 / Priya 4000

## Note
3 nights, paid on card. Receipt attached.
```
Columns: `expense_id, group, cost, currency, date, paid_by, shares_json, receipt_url, content_hash`. Emails hashed, not in prose.

## Implementation Details
```python
def splitwise_source(
    api_key: str | None = None,       # personal token, falls back to SPLITWISE_API_KEY
    group_ids: list[int] | None = None,
    include_groups: bool = True,
    include_balances: bool = True,
    max_expenses: int = 1000,
    client: httpx.AsyncClient | None = None,
):
    """Create a dlt source yielding Splitwise expenses as documents."""

@dlt.resource(name="splitwise_expenses", primary_key="id", write_disposition="merge")
def splitwise_expenses():
    # 1. Paginate limit/offset, filter group_ids client-side
    # 2. Hash-gate, yield doc rows; voided → _deleted=True
    # 3. Advance last_updated_at only on success
```

## Tests
| Test | Verifies |
|------|----------|
| `test_limit_offset_pagination` | All pages walked |
| `test_updated_at_cursor` | Old expenses skipped |
| `test_void_emits_deleted` | `deleted_at` → `_deleted` |
| `test_hash_skips_unchanged` | Re-sync 0 rows |
| `test_group_filter` | Non-selected group excluded |
| `test_email_not_in_prose` | PII columns-only |
| `test_document_source_attr` | Tagged `splitwise` |

## Example
```python
import asyncio, os, cognee
from cognee_community_connector_splitwise import splitwise_source


async def main():
    assert os.environ.get("SPLITWISE_API_KEY"), "Set SPLITWISE_API_KEY"
    source = splitwise_source()
    await cognee.remember(source, dataset_name="splits")
    print(
        await cognee.search(
            "Who still owes for Goa?",
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=["splits"],
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
```

## Dependencies
```toml
[project]
name = "cognee-community-connector-splitwise"
version = "0.1.0"
description = "Splitwise data-source connector for cognee — sync shared expenses into memory"
readme = "README.md"
requires-python = ">=3.11,<=3.13"
dependencies = ["cognee==1.4.0", "dlt[sqlalchemy]>=1.9.0,<2", "httpx>=0.27.0,<1"]
```

## Difficulty Assessment: Easy (E)
**Why Easy:** single token in v1, 3 endpoints, limit/offset pagination, no review, tiny schemas.
**Avoided:** OAuth1 signing from scratch (document pre-minted token), repayments settling logic (folded as text), multi-currency conversion (stored, not converted).
