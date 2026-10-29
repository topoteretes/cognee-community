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
| `resource_name` | required | scopes sync state and cleanup; one per Ramp business. Must be lowercase snake_case: dlt keeps the name as given for the state and normalizes it for the table, so anything else is refused rather than silently sharing a table |
| `since` | all history | ISO 8601 time; earlier transactions are not ingested |
| `lookback_days` | 60 | how far behind the cursor each run re-reads (see below) |
| `require_context` | `True` | skip transactions that are only an amount |
| `include_declined` | `False` | also ingest declined (and errored) transactions |
| `department_ids`, `user_ids`, `entity_id` | all | only sync these departments, card holders or entity. The first two take a **list**; a bare string is refused, because it would become a selection matching nothing and every stored transaction would then be forgotten |
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

Ramp answers an expired token with HTTP 404 `DEVELOPER_7002`, the same as a missing record, and
documents no "record deleted" code. The client renews the token on that answer, and a transaction
is only taken as gone when a `GET /transactions/<id>` answers 404 with Ramp's exact
`The requested <kind> (<id>) does not exist.` sentence naming *that* id. A 404 on a listing, a
404 about another record, or any other wording is an error that forgets nothing.

### Rate limits and incomplete runs

Ramp rate limits per app. A 429 is waited out up to twice, for at most 60 seconds each, and then
the run stops: cognee holds a process-wide dlt staging lock while a source yields, so a long
`Retry-After` is not slept through.

A run that runs out of `max_requests`, or that Ramp keeps rate limiting, is **not** an error. It
yields what it reached, saves its progress and returns, so the next run continues where it stopped.
The only signal is `source.cognee_sync_stats`, which then holds `failed_budget` or
`failed_rate_limit` and a non-zero `failed`. Read those stats if you need to know whether a sync
finished. Everything else (a refused token, a malformed listing, an unreachable API) raises, and
dlt commits neither rows nor state — so there is no `failed_auth` count to read: an auth failure
never produces a committed run at all.

### Limitations

- A memo edit, receipt removal or deletion on a transaction older than the lookback window is
  only seen once a listing covers it again (raise `lookback_days`, or change the selection to
  re-list everything).
- Each changed transaction with receipts costs one extra request. Receipts are listed for the
  whole business, so with a selection, receipts on other people's transactions cost a read each.
- State belongs to one Ramp app (`client_id`); after switching apps for the same business, pass
  `adopt_state_from`. State keeps a few short values per stored transaction (no bodies), rewritten
  on every load.
- A transaction whose own read keeps failing (a 403, a 422, an unrecognised 404) is retried on five
  runs, then only once a week. Its document is kept, and `cognee_sync_stats["failed"]` stays at one
  or more while it is being retried: that is not a broken sync.
- `resource_name` becomes a dlt table name. Two names differing only in case or punctuation
  (`Ramp_A`, `ramp-a`, `ramp a`, `ramp.a`) all normalize to one table while keeping separate state,
  so neither business's cleanup could reach the other's rows. Rather than leave that to the reader,
  anything dlt's naming would change is **refused** when the source is built.
- Sync state keeps one row id per listed transaction while a walk is in progress, so that a
  resumed listing knows what it has already seen (1,375 on the sandbox backfill). It is bounded by
  the lookback window and dropped once the listing completes.
- Anyone who can read the cognee dataset can read the transactions in it, including card holders'
  and attendees' names. Give the dataset the same audience as Ramp's transaction list.
- One malformed record in a listing is skipped and counted in `failed` rather than failing the run;
  the rest of the page still syncs.

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
