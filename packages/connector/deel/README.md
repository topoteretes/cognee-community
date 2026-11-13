# cognee-community-connector-deel

A [Deel](https://www.deel.com) data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync contract and worker-directory **metadata** into memory — "ask my HR data" — with
incremental sync and forget-on-delete.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each contract and person
becomes a small Markdown document that flows through cognee's cognify pipeline (cognee
"document-mode", like the Notion and Google Drive connectors).

> **Contracts and worker data are sensitive HR data.** Everything is allowlisted and
> opt-in; see [Privacy model](#privacy-model).

## Requirements

Needs a cognee release with document-mode (`DOCUMENT_SOURCE_ATTR` + `resolve_dlt_sources`
routing), first shipped in cognee 1.4.0 — pinned in `pyproject.toml`.

## Install

```bash
uv pip install cognee-community-connector-deel
# or, from this monorepo:
cd packages/connector/deel && uv sync --all-extras
```

## Prerequisites and setup

1. A Deel account. For testing use the sandbox (separate credentials from production).
2. An **organization API token** (Developer Center in the Deel app) with scopes
   `contracts:read` and `people:read`. Tokens are sent as `Authorization: Bearer <token>`.
3. Export credentials — never put the token in code:

   ```bash
   export DEEL_API_TOKEN="..."
   export DEEL_BASE_URL="https://api-staging.letsdeel.com/rest"   # sandbox
   # unset = production: https://api.letsdeel.com/rest
   export LLM_API_KEY="sk-..."
   ```

   The token can also be passed as `token=` or set as dlt secret `sources.deel.api_token`.

| Environment | Base URL |
| --- | --- |
| Production (default) | `https://api.letsdeel.com/rest` |
| Sandbox | `https://api-staging.letsdeel.com/rest` — per Deel's [sandbox docs](https://developer.deel.com/api/stable/sandbox.md) |

> The sandbox host is from Deel's docs. A sandbox organization token (scopes `contracts:read`,
> `people:read`) got HTTP 200 on this host and 401 on the production host, so tokens are
> environment-specific. The connector's sync was run live against the sandbox (224 contracts,
> 259 people, clean sweeps, stable hashes between runs); the cognify/search step with an LLM
> was not (see [Assumptions to verify](#assumptions-to-verify)).

## Usage

```python
import cognee
from cognee_community_connector_deel import deel_source

await cognee.remember(
    deel_source(),  # DEEL_API_TOKEN / DEEL_BASE_URL from the environment
    dataset_name="deel",
    primary_key="id",
    write_disposition="merge",  # required for incremental sync (see below)
    max_rows_per_table=0,
)

answer = await cognee.recall("Which teams have in-progress contracts?")
```

A runnable version, with notes on first run vs. incremental runs, is in
[`examples/example.py`](examples/example.py).

## Configuration

All parameters of `deel_source(...)`:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `token` | env `DEEL_API_TOKEN`, then dlt secret `sources.deel.api_token` | API token. Never logged. |
| `base_url` | env `DEEL_BASE_URL`, then production | API base URL. `DEEL_SANDBOX_URL` / `DEEL_PRODUCTION_URL` are exported. |
| `resources` | `["contracts", "people"]` | Which resources to ingest. |
| `contract_statuses` | none | Server-side `statuses` filter. Contracts leaving the filter are treated as deleted. |
| `contract_types` | none | Server-side `types` filter (same caveat). |
| `include_pii` | `False` | Also ingest worker names, emails and addresses. |
| `include_contract_documents` | `False` | Opt in to fetching contract documents. The documents endpoint is never called otherwise. |
| `document_max_bytes` | `5_000_000` | Skip documents larger than this. |
| `document_types` | text/plain, text/markdown, text/csv, application/pdf | Content-type allowlist for documents. |
| `documents_path` | `/eor/contracts/{contract_id}/documents` | Documents listing path (`{contract_id}` placeholder). |
| `page_size` | `100` | Page size; capped at 100, the documented maximum. |
| `overlap_seconds` | `3600` | Records whose `updated_at` is within this window of the cursor are re-emitted. |
| `full_reconcile` | `False` | Emit every record each run (plus tombstones), as a safety net. For a guaranteed purge of untracked records, also run `remember(..., write_disposition="replace")` periodically. |
| `max_delete_ratio` | `0.5` | Abort a run that would delete more than this fraction of known records. |
| `force_delete` | `False` | Override the delete guard (and the empty-response guard). |
| `drop_statuses` | none | Contract statuses to remove from memory. By default terminated/cancelled contracts stay as status updates. |
| `skipped_table` | `False` | Also load skipped-record ids and reasons into a `deel_skipped` table. Off by default because cognee ingests every table of a document source. |
| `session` | built-in | Pre-built `requests` session (test-injection point). |

## Supported resources

| Resource | Endpoint | Table | Document id |
| --- | --- | --- | --- |
| Contracts | `GET /contracts` (cursor pagination) | `deel_contracts` | `deel:contract:{id}` |
| People | `GET /people` (offset pagination) | `deel_people` | `deel:person:{id}` |
| Contract documents (opt-in) | `GET /eor/contracts/{id}/documents` | `deel_contract_documents` | `deel:contract-document:{contract}:{doc}` |

Ids are deterministic, so re-syncs update documents in place and never duplicate them.

## How sync works

Each run pages through a resource's list endpoint **once**. During that pass it emits the
records that changed *and* collects every id it saw — emission and the delete sweep share
the same requests.

**Change detection.** Neither list endpoint has a server-side `updated_at` filter, so
filtering is client-side: **every run fetches the full list** (about `ceil(n / 100)`
requests, throttled to 5 req/s). The connector keeps a short content hash of each record's
allowlisted fields in dlt resource state (`id -> "hash:first_seen"`) and emits only records
whose hash changed; unchanged records are not re-sent to cognify.

Records that carry `updated_at` also feed a cursor (the newest value seen). Records within
`overlap_seconds` of the cursor are re-emitted each run; the `merge` on `id` absorbs them as
no-ops and their content is identical, so cognee does not re-process them. Expect a short
tail of "re-sent" records right after a burst of edits. Set `overlap_seconds=0` to re-send
only the record sitting exactly at the cursor. (The documented contracts schema lists no
`updated_at`, but the sandbox returns one; the hash is the real change signal either way.)

State is only written when the whole pass completes, so a failed run resumes without
skipping records.

**State size.** `known` costs roughly 65 bytes per record (about 0.7 MB per 10 000
records); dlt stores state compressed in the destination.

## Deletion semantics

A record known from earlier runs but absent from this run's pass is emitted as a hard-delete
tombstone (`_deleted=True`). dlt removes the row from the destination and cognee's
`orphan_cleanup` removes the document from the graph, vector and relational stores. That
only happens when the pass was **clean**. No tombstones are emitted, and the run is logged
as `sweep=incomplete:<reason>`, when:

- paging hit a 429/5xx after retries, or a network error (`http_<status>`, `network_error`);
- a page was truncated (`fewer records than page.total_rows`) or repeated (`truncated`, `repeated_page`);
- the response was empty although records are known (`empty_response` — typically a token
  that lost its scope; override with `force_delete=True`);
- a record had no usable id (`record_without_id`).

401/403 mid-run raise `DeelAuthError` and change nothing.

**Delete guard.** If a clean pass would delete more than `max_delete_ratio` of the known
records, the run aborts with `DeelDeleteGuardError` and nothing is changed. Re-run with
`force_delete=True` if the deletion is real. Note: with a single known record, deleting it
upstream looks like an empty response, so use `force_delete=True` for that case too.

**Limits.** The sweep can only see what the token and filters can see. Narrowing
`contract_statuses`/`contract_types`, or losing visibility of a team, deletes those
records from memory — the guard exists for exactly that. If dlt state is lost, previously
loaded records can no longer be tombstoned; `full_reconcile=True` re-emits everything but
cannot discover ids it has never seen.

## Write disposition: `merge`, with a safe fallback

`resolve_dlt_sources` reads back **all** rows of the dlt destination after a load and
`_delete_dlt_orphans` (`cognee/tasks/ingestion/resolve_dlt_sources.py`) deletes every
cognee `Data` item of this source whose id is not in that set, via
`delete_data_nodes_and_edges` + `delete_data`. Consequences:

- `merge` on `id` + a `hard_delete` column (`_deleted`) is the right model for an
  incremental source: unchanged rows stay in the destination, and a tombstone removes the
  row, which makes it an orphan and purges it from the graph. This is the same mechanism
  Google Drive and Gmail use, and Drive's forget test covers it end to end.
- `replace` is only correct for a **full snapshot** (Notion, Slack). Under `replace`, any
  record that is not re-emitted is purged. An incremental source must never run under it.

cognee's default `write_disposition` is `replace`, and a value passed to `remember` overrides
the resource's own hint. So the connector reads the *effective* disposition at run time: if
it is not `merge` it switches to a full snapshot pass (every record, no tombstones, absence
is the delete signal), which is safe but slower. Pass `write_disposition="merge"` in your
`remember` call for incremental behaviour.

**`full_reconcile` and the guaranteed delete path.** Hard-deletes only reach the graph through
`orphan_cleanup`, so a periodic full reconcile is the safety net:

- `full_reconcile=True` under `merge`: every record is re-emitted (plus tombstones). Use it
  to re-sync everything the connector knows about.
- `remember(..., write_disposition="replace")` (with any `full_reconcile` value): the staging
  table is rewritten with exactly the records Deel returns now, so anything that is no
  longer there, including ids the connector lost track of (for example after losing its
  state), is purged by `orphan_cleanup`. Run this occasionally, e.g. weekly.

## Privacy model

- **Allowlist.** Per resource, only named fields are read: ids, type/status, title, team /
  legal entity / department, seniority, job title, country/state, employment type and
  dates. A new field in the API response never reaches a document automatically.
- **`include_pii=False` (default)** drops worker name, email, and address.
  `include_pii=True` adds them. **Never included, even then:** compensation and payment
  data, birth dates, nationalities, government/tax ids (`additional_details`), signatures,
  invitation emails.
- **Contract titles are free text** and may contain a worker's name; the connector cannot
  scrub them.
- **Contract documents** are fetched only with `include_contract_documents=True`, lazily
  and per changed contract, within `document_max_bytes` and the `document_types`
  allowlist. Failures are skipped with a reason; content is never logged. The bearer token
  is sent only to the Deel host, never to other hosts (e.g. pre-signed storage URLs).
  Documents refresh when their contract changes or with `full_reconcile=True`.
- **Logging.** Logs contain counts and, for skipped records, opaque ids and fixed-vocabulary
  reasons — never tokens or record content.
- **Skipped records** are logged (record id and reason only), counted in the run summary, held in
  resource state, and loaded to `deel_skipped` (ids and reasons only) when `skipped_table=True`.

## Rate limits and retries

Deel allows 5 requests/second per organisation, shared across tokens, and returns **no
rate-limit headers and no `Retry-After`** ([docs](https://developer.deel.com/api/rate-limits.md)).
The list endpoints are configured through dlt's declarative `rest_api` source (endpoint, bearer
auth, pagination, params); single calls (startup check, documents) use dlt's `RESTClient`.
They share one session that retries 429 and 5xx (and network
errors) up to 5 attempts with exponential backoff (honouring `Retry-After` if one is ever
sent), behind a client-side throttle of one request per ~0.21 s plus a little jitter.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `DeelAuthError … HTTP 401` | Token invalid/expired, or sandbox token used against production (or vice versa). |
| `DeelAuthError … HTTP 403` | Token lacks `contracts:read` / `people:read`. |
| `DeelDeleteGuardError` | The run would delete more than `max_delete_ratio` of known records. Check filters/scopes; `force_delete=True` if intended. |
| `sweep=incomplete:…` in logs | Deletes were skipped for safety; nothing is lost. It self-heals on the next clean pass. |
| Every record re-sent each run | You are running under `replace`; pass `write_disposition="merge"`. |
| Only 50 rows ingested | Pass `max_rows_per_table=0` to `remember`. |

## Assumptions to verify

Not confirmed against a live or sandbox Deel account (highest risk first):

1. **Verified live (sandbox, sync only, no LLM):** base URL `https://api-staging.letsdeel.com/rest`;
   bearer auth; both scopes; cursor pagination of contracts (224 records) and offset pagination
   of people (259 records) at page size 100 with `page.total_rows` matching; stable hashes
   across runs. Verified offline through cognee's real `add` path (no LLM): documents are created
   and a contract deleted upstream is removed from the cognee dataset on the next sync.
   Also verified live (read-only): narrowing the `contract_types` filter made 58 of 224 contracts
   stop being returned, and the next sync tombstoned exactly those 58 and removed their rows.
   A burst of 40 requests from 10 workers (throttle off) drew no 429s, so real 429 behaviour was
   not observed; retry/backoff is covered by offline tests only.
   **Not verified:** cognify + search with an LLM, deleting a record in Deel itself (the token
   is read-only), and any non-sandbox (production) account.
2. **Contracts `updated_at`**: absent from the documented schema but returned by the sandbox
   (observed). The list has no server-side change filter. The hash path does not depend on it.
3. **Behaviour for deleted contracts** in `GET /contracts` is undocumented. The design
   assumes a deleted contract disappears from the list. No deletion feed or deletion webhook
   is documented (examples show `contract.created/signed/terminated` only).
4. **Contract documents**: the only documented listing is EOR-only and returns metadata, with
   no download link. The connector reads a download link from `download_url` / `url` if the
   response has one (assumption); otherwise the document is skipped as `no_download_url`.
5. **Maximum page size 100** is from the best-practices page; the endpoint reference does not
   state a maximum (contracts default 50). A smaller real maximum would show as a short
   page and be caught by the `truncated` check (deletes skipped).
6. **People `updated_at`** is documented as nullable; its reliability is unknown. Hash
   detection does not depend on it.
7. **Pagination end markers**: the connector stops on an empty page, a missing or repeated
   cursor, or `offset >= total_rows`; the exact end-of-list response was not observed.
8. **Cursor field names** (`page.cursor`, `after_cursor`, `page.total_rows`) are from the
   endpoint reference.

## Testing

```bash
uv run pytest tests/
```

The tests run with no network or credentials: a fake Deel API is served through the real
dlt retrying session and `rest_api` source, and loads go to a temporary sqlite destination. They
cover pagination, the incremental hash/cursor/overlap behaviour, failed-run resumption,
single-pass tombstones, partial/empty sweeps, the delete guard, privacy (allowlist,
`include_pii`, documents opt-in), retries, bad records, startup validation, log hygiene and
the write-disposition fallback. `tests/test_cognee_integration.py` runs cognee's real `add`
path (no LLM) and checks that documents are created and that a contract deleted upstream is
removed from the dataset on the next sync.

Two read-only live checks are in `examples/`: `check_sandbox.py` (two syncs plus a simulated
upstream deletion into a throwaway database) and `check_rate_limit.py` (a request burst to see
real 429s and the retry behaviour). Neither needs an LLM.
