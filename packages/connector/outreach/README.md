# cognee-community-connector-outreach

An [Outreach](https://www.outreach.io) data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync **sequences, prospects, sequence
memberships and sales activity** into memory — "ask my Outreach" — with incremental sync and
forget-on-delete.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each record becomes a small
Markdown document that flows through cognee's cognify pipeline (cognee "document-mode", like
the Notion, Google Drive and Deel connectors). All HTTP goes through dlt's declarative
`rest_api` source — there are no hand-rolled request loops.

> ## Testing status — please read
>
> **Tested with mocked HTTP only. NOT tested against a live Outreach account.**
> Outreach partner approval was pending when this was written, so every request/response
> shape comes from Outreach's public documentation plus assumptions. The 82 tests run a fake
> Outreach API through the real dlt session, `rest_api` source, OAuth class and cognee's real
> `add` path (no LLM). Before relying on this, run
> [`examples/verify_live.py`](examples/verify_live.py) with real credentials and work through
> [Assumptions to verify](#assumptions-to-verify).

> **Prospect data is personal data.** Everything is allowlisted; contact details and message
> bodies are opt-in. See [Privacy model](#privacy-model).

## How it works

* **JSON:API.** Outreach returns `data / attributes / relationships / links / meta`. A pure
  function (`flatten_resource`) flattens each resource: `id`, `type`, snake_case attributes,
  relationships as `<name>_id` / `<name>_ids` (read from the inline `data`; never one extra call
  per record), timezone-aware `created_at` / `updated_at`, and `url` from `links.self`.
* **Documents.** Each flattened record becomes one row with a Markdown `content` column plus
  `id` (`outreach:<type>:<id>`, e.g. `outreach:prospect:42`), `title`, `url`, `updated_at` and
  the key relationship ids as columns. `DOCUMENT_SOURCE_ATTR` is set to `"outreach"` on the
  source, which routes the rows through normal cognify instead of cognee's relational path.
* **Incremental cursor.** Per table, `cursor_updated_at` lives in dlt resource state. Each run
  requests `filter[updatedAt]=<cursor − overlap_seconds>..inf&sort=updatedAt` with sparse
  fieldsets and `count=false`, following `links.next`. The overlap (default 1 s) makes the
  lower bound safe for records that share the cursor's timestamp or straddle a page boundary;
  `merge` on `id` absorbs the re-sent rows. The cursor is committed only when the run's
  generator finished, together with dlt's load package, so a failed run or failed load leaves it
  untouched. Timestamps in the future are clamped to "now" so clock skew cannot skip data.
* **Deletes (the id sweep).** The `updatedAt` filter cannot show deletions and the REST API has
  no delete feed. So every `sweep_every_n_runs` runs (default: every run) a separate **id-only**
  pass (`fields[type]=updatedAt`, no bodies) lists what still exists. `known ids − sweep ids`
  become `_deleted` tombstones. Safety rules:
  * tombstones are emitted **only if the sweep completed cleanly** — no pagination fault, no
    exhausted 429/5xx retries, no 401/403 (e.g. a revoked scope), and not an implausible empty
    result while records are known; otherwise the run records `sweep = incomplete:<reason>`
    and emits nothing (the next clean sweep catches up);
  * a sweep that would delete more than `max_delete_ratio` (default 50 %) of known records
    **aborts the run** with `OutreachDeleteGuardError` and loads nothing, unless
    `force_delete=True`;
  * `known` ids are kept in dlt state as a compressed blob (id + updated-at epoch), ~a few bytes
    per record, not a plain list.
* **Activity window.** Activity (mailings, calls, tasks, meetings) is bounded by
  `activity_since` (default 90 days; first-sync lookback *and* retention window). Sweeps only
  cover that window, and ids that age out of it are dropped from state — so deleting an activity
  record **after** it left the window is never propagated (see [Limits](#sync-and-deletion-semantics-and-limits)).

## Install

```bash
uv pip install cognee-community-connector-outreach
# or, from this monorepo:
cd packages/connector/outreach && uv sync --all-extras
```

Requires a cognee release with document-mode (`DOCUMENT_SOURCE_ATTR` + `resolve_dlt_sources`),
first shipped in cognee 1.4.0 — pinned in `pyproject.toml`. Only cognee and dlt are
dependencies; dlt supplies the REST client, retrying session, `rest_api` source and pagination.

## Prerequisites and setup

1. **An Outreach OAuth app.** Outreach apps are created in the Outreach developer portal
   (<https://developers.outreach.io/>). *Partner/app approval may be required before an app can
   be used against a production account* — this connector was written before that approval.
2. **Redirect URI.** Register `http://localhost:8765/callback` (or your own loopback URI and pass
   `--redirect-uri` to `outreach-auth`).
3. **Scopes.** Enable read access for what you sync (names follow Outreach's `<resource>.read`
   form; see `STREAMS[...].scopes` and [Assumptions](#assumptions-to-verify)):

   | Resource | Scopes (assumed) |
   | --- | --- |
   | sequences | `sequences.read` |
   | prospects (+ memberships) | `prospects.read`, `sequenceStates.read` |
   | activity | `mailings.read`, `calls.read`, `tasks.read`, `meetings.read` |

4. **Credentials** — never put them in code:

   ```bash
   export OUTREACH_CLIENT_ID="..."
   export OUTREACH_CLIENT_SECRET="..."
   export LLM_API_KEY="sk-..."
   ```

   They can also be passed as `client_id=` / `client_secret=` or set as dlt secrets
   `sources.outreach.client_id` / `sources.outreach.client_secret`.
5. **Authorize once:** `uv run outreach-auth` opens the consent page, receives the redirect on a
   loopback server (state-checked), exchanges the code, and writes the tokens to
   `~/.config/cognee/outreach_tokens.json` (mode `0600`; override with `--token-store` or
   `OUTREACH_TOKEN_STORE`). The tokens are never printed.

## Usage

```python
import cognee
from cognee_community_connector_outreach import outreach_source

await cognee.remember(
    outreach_source(),
    dataset_name="outreach",
    primary_key="id",
    write_disposition="merge",  # incremental + deletions; see "write_disposition" below
    max_rows_per_table=0,  # let orphan cleanup see the whole corpus
)
answer = await cognee.recall("Which prospects are in active sequences?")
```

Run the full flow with [`examples/example.py`](examples/example.py).

### Configuration (`outreach_source`)

| Parameter | Default | Meaning |
| --- | --- | --- |
| `resources` | all | Subset of `sequences`, `prospects`, `activity`. `prospects` also loads sequence memberships (`/sequenceStates`). |
| `sequence_ids` | `None` | Restrict sequences and memberships to these sequence ids. Prospects themselves are not filtered. |
| `sequence_states` | `None` | Restrict memberships to these states (e.g. `["active", "paused"]`). |
| `activity_types` | all four | Subset of `mailings`, `calls`, `tasks`, `meetings`. |
| `activity_since` | 90 days | First-sync lookback **and** retention window for activity (`timedelta` or days). |
| `include_contact_details` | `False` | Also ingest emails, phones, postal addresses. |
| `include_activity_bodies` | `False` | Also ingest email subject/text, call/task notes, meeting title/description. |
| `page_size` | 100 | Records per request, capped at `MAX_PAGE_SIZE` (1000). |
| `overlap_seconds` | 1 | Subtracted from the stored cursor every run. |
| `sweep_every_n_runs` | 1 | Run the id-only delete sweep every N runs. |
| `full_reconcile` | `False` | Re-send everything each run and derive deletes from that listing. |
| `max_delete_ratio` | 0.5 | A sweep deleting more than this share of known records aborts the run. |
| `force_delete` | `False` | Allow a sweep above `max_delete_ratio` (and an empty result). |
| `skipped_table` | `False` | Also load `outreach_skipped` (id / type / reason). Off by default — cognee ingests every table of a document source. |
| `base_url` | API v2 | Or env `OUTREACH_BASE_URL`. |
| `client_id`, `client_secret` | env / dlt secrets | OAuth app credentials. |
| `token_store` | `~/.config/cognee/outreach_tokens.json` | Or env `OUTREACH_TOKEN_STORE`. |

## Supported resources

| `resources` | dlt table(s) | Endpoint(s) (assumed) |
| --- | --- | --- |
| `sequences` | `outreach_sequences` | `/sequences` |
| `prospects` | `outreach_prospects`, `outreach_sequence_states` | `/prospects`, `/sequenceStates` |
| `activity` | `outreach_mailings`, `outreach_calls`, `outreach_tasks`, `outreach_meetings` | `/mailings`, `/calls`, `/tasks`, `/meetings` |

Sequence membership (prospect ↔ sequence, with state) is its own document per `sequenceState`
carrying both ids, so the link is in the graph without an extra request per prospect.

## Privacy model

* Per record type there is an **explicit attribute allowlist** (`STREAMS` in `outreach.py`).
  Attributes not listed — including any Outreach adds later — never reach a document, and are
  not even requested (sparse fieldsets).
* **Contact fields** (emails, all phone numbers, postal address parts; a call's `to`/`from`)
  are dropped unless `include_contact_details=True`. Prospect **names**, titles and companies
  are ingested by default.
* **Bodies** (email subject/text, call and task notes, meeting title/description) are dropped
  unless `include_activity_bodies=True`. HTML bodies are never ingested.
* Tokens, client secret and record content are **never logged**; log lines carry counts and
  opaque ids only. Skip reasons come from a fixed vocabulary (`missing_id`,
  `invalid_timestamp:updated_at`, …).
* The token file is `0600` and written atomically; the bearer token is only ever sent to the
  configured API host (a `links.next` pointing elsewhere aborts the pass).
* Use a dedicated dataset so everything can be wiped with one `cognee.prune`.

## Sync and deletion semantics and limits

* First run: full fetch (activity: last `activity_since`). Later runs: changes since the cursor.
* An edited record is re-sent and re-cognified; unchanged ones are not.
* A record deleted upstream disappears from memory on the next run that performs a clean sweep
  (the next run, with the default `sweep_every_n_runs=1`).
* **Limits:** activity outside `activity_since` is not swept; deleting the *last remaining*
  record of a table is not propagated (cognee skips cleanup when the read-back is empty, and this
  connector distrusts an empty sweep unless `force_delete=True`); records that become malformed
  are skipped, not deleted; changing `sequence_ids` / `sequence_states` / `activity_types` makes
  out-of-scope ids look deleted (the guard will usually stop a large change — use
  `force_delete=True` deliberately); 50 % is a heuristic, not a guarantee.

### `write_disposition`: `merge` (default intent) with a `replace` fallback

* **Evidence (cognee 1.4.0 source, read in this repo's venv):**
  `cognee/tasks/ingestion/resolve_dlt_sources.py` passes the caller's `write_disposition` to
  `ingest_dlt_source` for document sources with `max_rows_per_table=0` (read back the *whole*
  staging table), then `_delete_dlt_orphans` deletes every `Data` item of that source tag whose
  `data_id` is not in the fresh read-back, via `delete_data_nodes_and_edges` + `delete_data`
  (graph, vector and relational stores). A `merge` row with `_deleted=True` and
  `hard_delete` is removed from staging by dlt, so it falls out of the read-back and is
  forgotten. This is covered end to end by `tests/test_cognee_integration.py`
  (`cognee.add` → delete upstream → record gone) — with the LLM `cognify` step not run.
* **`merge`** is what makes sync incremental (only changed rows are loaded; the sweep supplies
  the missing delete feed). Google Drive's connector uses the same mechanism with a real
  delete feed.
* **`replace`** is cognee's default when `write_disposition` is not passed. Under `replace` every
  row not re-sent is forgotten, which would wipe unchanged records. The source therefore reads
  the **effective** disposition at run time; if it is not `merge` it performs a **full snapshot**
  (no `updatedAt` filter, no tombstones — vanished records drop out of staging and are forgotten
  by `orphan_cleanup`). A partial snapshot (failed page) aborts the run rather than forgetting
  live data. Caveats: every record is re-sent each run, and under `replace` activity older than
  `activity_since` is forgotten too (it is a retention window).
* `full_reconcile=True` forces the same full pass under `merge`, plus tombstones derived from it.

## Rate limits and resilience

* Outreach's public docs do not state a rate limit; the connector throttles to
  `RATE_LIMIT_PER_HOUR = 10_000` (≈ one request per 0.36 s, with jitter) — an assumption.
* 429/5xx are retried with exponential backoff, honouring `Retry-After` (dlt's retrying
  session). An exhausted retry budget in the data pass fails the run (state untouched); in the
  sweep it only suppresses deletions.
* The **token endpoint is called once, never retried**: a retried refresh after the server
  already rotated the refresh token would present the dead one.
* Empty, repeated or looping pages, and non-JSON bodies, are treated as faults.
* A malformed record is skipped and counted (reason + id, never content) and the sync continues.
  Each stream logs `fetched / emitted / skipped / tombstoned / sweep status`, which is also kept in
  dlt state under `last_run`.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `No Outreach refresh token in … Run outreach-auth` | Never authorized, or `OUTREACH_TOKEN_STORE` points elsewhere. |
| `rejected the token refresh … Re-run outreach-auth` | Refresh token revoked/expired, wrong client secret, or it was rotated by another process whose write was lost. |
| `HTTP 403 … missing a scope` | Enable the named scope on the OAuth app, re-run `outreach-auth`. |
| `HTTP 400` on the first list call | An assumed field/filter name is wrong — run `verify_live.py`. |
| `OutreachDeleteGuardError` | A sweep would delete > `max_delete_ratio`; check scope/filters, then `force_delete=True` if intended. |
| `sweep = incomplete:…` in the log | A transient/scope fault during the sweep; no deletions were made. |
| Everything re-sent every run | `write_disposition="merge"` was not passed (snapshot fallback). |

## Testing

```bash
uv run pytest tests/ --cov=cognee_community_connector_outreach
```

Covers: JSON:API flattening, multi-page `links.next` pagination, cursor reuse and
only-changes emission, equal-timestamp page boundaries, cursor not advanced on failed pass /
failed load, token refresh on expiry and on 401 (single retry), refresh-token rotation
persisted before use (crash simulation), sweep tombstones, incomplete / 403 / empty sweeps
emitting no deletes, `max_delete_ratio` + `force_delete`, privacy defaults and opt-ins,
allowlist, no secrets/content in logs, 429 `Retry-After`, 5xx backoff, bad-record skipping,
`replace` fallback and `full_reconcile`, startup validation, the `outreach-auth` flow, the
`verify_live.py` script, and an end-to-end `cognee.add` forget-on-delete test.

## Assumptions to verify

Confirmed from Outreach's public docs (<https://developers.outreach.io/api/making-requests/>,
`/api/oauth/`): base URL, JSON:API content type, `filter[updatedAt]=a..inf` syntax, `sort`,
`fields[type]`, `page[size]` + `count=false` + `links.next`, authorize/token URLs, 7200 s token
lifetime, `refresh_token` grant parameters, dot-notation scopes.

**Everything else is unverified.** `examples/verify_live.py` checks most of it (read-only,
prints PASS/WARN/FAIL, never tokens or content). Ranked by risk:

- [ ] **Allowlisted attribute / relationship names** (all camelCase names in `STREAMS`) — a wrong
  name may return 400 (fieldset) or silently produce thin documents. → `STREAMS[...].attributes /
  contact / bodies / relationships`.
- [ ] **Sparse fieldsets accept relationship names** and still return relationship ids; also that
  `fields[type]=updatedAt` is valid for the sweep. → `SPARSE_FIELDSETS_INCLUDE_RELATIONSHIPS`,
  `SWEEP_FIELDS`.
- [ ] **JSON:API type names** used in `fields[...]` (`sequence`, `prospect`, `sequenceState`,
  `mailing`, `call`, `task`, `meeting`). → `STREAMS[...].api_type`.
- [ ] **Endpoint paths**, notably `/sequenceStates` and the four activity endpoints. →
  `STREAMS[...].path`.
- [ ] **Scope names** (`prospects.read`, `sequenceStates.read`, …). → `STREAMS[...].scopes` (also
  what `outreach-auth` requests by default via `ALL_SCOPES`, overridable with `--scopes`).
- [ ] **`filter[updatedAt]` lower bound is inclusive** and accepts millisecond ISO timestamps. →
  `TIMESTAMP_FORMAT`, `overlap_seconds`.
- [ ] **Refresh-token rotation** (undocumented) and whether `redirect_uri` is required on refresh. →
  `SEND_REDIRECT_URI_ON_REFRESH`.
- [ ] **Relationship / list filters**: `filter[id]=1,2`, `filter[sequence][id]=…`,
  `filter[state]=…`. → `PARAM_ID_FILTER`, `PARAM_SEQUENCE_FILTER`, `PARAM_SEQUENCE_STATE_FILTER`.
- [ ] **Max `page[size]` for cursor pagination** (docs state 1000 only for the deprecated offset
  mode). → `MAX_PAGE_SIZE`, `DEFAULT_PAGE_SIZE`.
- [ ] **Rate limit** (~10,000 req/h) and 429 / `Retry-After` behaviour. → `RATE_LIMIT_PER_HOUR`,
  `MAX_ATTEMPTS`.
- [ ] **Deleted records are simply absent** from list endpoints (no soft-delete flag). The sweep
  relies on this.
- [ ] Whether `links.self` is a useful `url` (it is the API URL, not an app URL).

## Live verification

```bash
uv run outreach-auth
uv run python examples/verify_live.py                 # read-only checks
uv run python examples/verify_live.py --force-refresh # also exercise refresh + rotation
```
