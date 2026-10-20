# cognee-community-connector-ramp

A Ramp data-source connector for [cognee](https://github.com/topoteretes/cognee): sync card
transactions, their memos and receipts into memory, so questions like "what did we spend on
the Acme offsite, and who was there?" have something to recall.

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Each transaction is ingested as
a document (cognee's document mode), so it goes through cognify entity extraction.

## Requirements

Python 3.10 to 3.14, and cognee 1.6.4 or later (for per-row node sets).

## Install

```bash
uv pip install cognee-community-connector-ramp
# or, from this monorepo:
cd packages/connector/ramp && uv sync
```

## Setup

1. In Ramp, an admin creates a developer app (Company → Developer → Apps), keeps client
   credentials on, and under *Configure allowed scopes* allows only `transactions:read` and
   `receipts:read`.
2. Pass its `client_id` and `client_secret` (and `sandbox=True` for `demo-api.ramp.com`).

## Usage

Inside an async function (or a notebook):

```python
import cognee
from cognee_community_connector_ramp import ramp_source

await cognee.remember(
    ramp_source(
        resource_name="ramp_acme",  # one per Ramp business; keep it fixed
        client_id="...",
        client_secret="...",
    ),
    dataset_name="ramp",
    primary_key="id",
    write_disposition="merge",  # required: the add pipeline defaults to "replace"
    self_improvement=False,  # no enrichment pass on every scheduled sync
)
```

See `examples/example.py` for the full flow, including a `GRAPH_COMPLETION` search.

| Argument | Default | |
| --- | --- | --- |
| `resource_name` | required | scopes sync state and cleanup; one per Ramp business |
| `since` | all history | ISO 8601 time; earlier transactions are not ingested |
| `lookback_days` | 60 | how far behind the cursor each run re-reads (see below) |
| `require_context` | `True` | skip transactions that are only an amount |
| `include_declined` | `False` | also ingest declined (and errored) transactions |
| `department_ids`, `user_ids`, `entity_id` | all | only sync these departments, card holders or entity |
| `max_requests` | 1000 | request budget of one run; a big backfill finishes over several runs |
| `adopt_state_from` | `None` | `client_id` of a previous Ramp app of the same business, whose state this run may take over |

Changing any of the selection arguments makes the next run list everything again: newly included
transactions are ingested, and stored ones outside the selection are forgotten.

## What gets ingested

One document per transaction, written around the memo:

```
Memo: "Team offsite dinner - Q4 planning with Acme"
Sam Lee (Engineering) paid Nopa (San Francisco, CA, US) on 2026-10-12: Restaurants, 482.10 USD, cleared.
Trip: SF offsite
Attendees: Jane Doe, Raj Patel
Receipt: 2 line items (Burrata, Halibut)
Accounting: GL account: Meals
```

Line-item memos, policy notes and dispute notes are included too. Each document carries the node
set `ramp:department:<name>`, so a search can be scoped to one department. Receipt images and
card numbers are not ingested. Documents do carry card holders' and attendees' names, so give the
cognee dataset the same audience as Ramp's transaction list.

## How sync works

`GET /transactions` only filters by `user_transaction_time`, but memos, receipts and settlement
arrive days later. So each run re-lists a `lookback_days` window behind the cursor, oldest first.
Transactions whose fields are unchanged are skipped before their receipts are fetched, and only
documents whose text changed are re-ingested. A receipt added to an older transaction is caught by
a `created_after` cursor on receipts and re-read by id; a receipt Ramp hasn't run OCR on yet is
fetched again for 3 days. The cursor never moves past the run's start, since Ramp has
future-dated transactions.

Ramp has no delete feed. A stored transaction missing from a complete listing (an expired
authorization, a decline) is re-read by id; if it is gone, declined, out of the selection or left
without context, it is emitted with the `_deleted` hard-delete marker and cognee forgets it.

Errors never delete. Token errors and failed or malformed listings abort the run, and dlt
commits neither its rows nor its state. An error on a single transaction is counted in
`cognee_sync_stats["failed"]` and retried on later runs; a record that keeps hitting its own
error (a 403, 422 or unrecognised 404) for five runs is then re-read only weekly, and its
document is kept. A Ramp outage (5xx, network) is retried without limit. A run stopped by
`max_requests` or a 429 resumes next time; if the saved page token's record has since
disappeared, Ramp answers `422 "Invalid start token"` and the listing restarts.

Ramp answers an expired token with HTTP 404 `DEVELOPER_7002`, the same as a missing record. The
client renews the token on that answer, and only a 404 naming the requested record counts as
deleted.

### Limitations

- A memo edit, receipt removal or deletion on a transaction older than the lookback window is
  only seen once a listing covers it again (raise `lookback_days`, or change the selection to
  re-list everything).
- Each changed transaction with receipts costs one extra request. Receipts are listed for the
  whole business, so with a selection, receipts on other people's transactions cost a read each.
- State belongs to one Ramp app (`client_id`); after switching apps for the same business, pass
  `adopt_state_from`. State keeps a few short values per stored transaction, rewritten on every
  load.

## Verified against the Ramp sandbox

Run through `cognee.add` against `demo-api.ramp.com` on 2026-10-20:

| Run | Result |
| --- | --- |
| First sync | 1,375 non-declined transactions, 1,288 documents (87 amount-only skipped), 0 failed, 125 requests |
| Second sync | 0 emitted, the same 1,288 document ids, 53 requests (sandbox receipts never get OCR, so recent ones are re-fetched for 3 days) |
| `include_declined=True` | +40 declined transactions (1,328 documents) |
| back to the default | the 40 are forgotten, leaving exactly the first sync's documents |

## Testing

```bash
uv run pytest tests/
```

No network or LLM key needed; the fake API in `tests/fake_ramp.py` serves the response shapes
recorded from the sandbox. `tests/test_ramp_forget.py` runs the cognee pipeline end to end with a
mocked LLM.
