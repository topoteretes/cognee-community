# cognee-community-connector-smartsheet

A Smartsheet data-source connector for [cognee](https://github.com/topoteretes/cognee): sync
sheets, rows, comments, and attachment metadata into memory.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each sheet and each
content-bearing row becomes a **normal document** in cognee (it flows through chunking and LLM
entity extraction), via cognee's document-mode marker.

## Requirements

- cognee **1.6.3** (pinned). The connector relies on table-scoped document cleanup
  (`DOCUMENT_SYNC_VERSION`), `PIPELINE_SCOPE_ATTR`, and `guarded_rows`.
- Python 3.11 to 3.13.
- A Smartsheet account on a **Business, Enterprise, or Advanced Work Management** plan. The
  free trial and Pro plan cannot generate API access tokens (confirmed:
  [help.smartsheet.com/articles/529590](https://help.smartsheet.com/articles/529590), *"You
  can't generate API access tokens"* during the trial).

## Install

```bash
uv pip install cognee-community-connector-smartsheet
# or, from this monorepo:
cd packages/connector/smartsheet && uv sync
```

## Setup

1. Sign in to Smartsheet on a Business, Enterprise, or Advanced Work Management plan.
2. Generate a personal access token: click your profile icon → **Personal Settings** → **API
   Access** → **Generate new access token**. Copy it immediately — it is shown only once.
3. **A token is not scopable.** It inherits every permission the creating user has — "they will
   be able to access all Smartsheet data that you have access to, both to read and modify on
   your behalf" (developers.smartsheet.com auth guide). **Strongly consider creating a
   dedicated account just for this connector, shared only on the sheets you actually want
   ingested**, rather than generating a token from your main account.
4. Note your account's region if it is not the US default: EU, AU, and US Government accounts
   use a different host (see "Host / region" below). Tokens are region-specific; the wrong
   host fails authentication outright, it does not redirect you.
5. Set your `LLM_API_KEY` like any other cognee run.

```bash
export SMARTSHEET_API_TOKEN=...
export SMARTSHEET_SHEET_IDS=1234567890123456          # comma-separated
uv run python examples/example.py
```

### Host / region

Pass `host=` (or set `SMARTSHEET_HOST`) if your account is not on the US region:

| Region | Host |
|---|---|
| US (default) | `https://api.smartsheet.com` |
| EU | `https://api.smartsheet.eu` |
| Australia | `https://api.smartsheet.au` |
| US Government | `https://api.smartsheetgov.com` |

Each region is a fully separate, isolated deployment — a token from one region cannot be used
against another.

## Usage

```python
import cognee
from cognee_community_connector_smartsheet import smartsheet_source

await cognee.remember(
    smartsheet_source(
        sheet_ids=[1234567890123456]
    ),  # or folder_ids=[...] / workspace_ids=[...] / all_sheets=True
    dataset_name="my_sheets",
    primary_key="id",
    write_disposition="merge",  # required
    max_rows_per_table=0,  # no row cap; a sheet exceeds the default 50
)

answer = await cognee.recall("What's on my project tracker sheet?", datasets=["my_sheets"])
```

Run the same `remember` call again whenever you want to sync. See `examples/example.py` for
the full flow.

| Argument | Env var | Default |
|---|---|---|
| `api_token` | `SMARTSHEET_API_TOKEN` | required |
| `host` | `SMARTSHEET_HOST` | `https://api.smartsheet.com` (US) |
| `sheet_ids` | — | — |
| `folder_ids` | — | — (recurses into subfolders) |
| `workspace_ids` | — | — (recurses into folders) |
| `all_sheets` | — | `False` — every sheet the token can see; explicit opt-in |
| `include_columns` / `exclude_columns` | — | all columns included; mutually exclusive |
| `include_comments` | — | `True` |
| `include_attachments` | — | `True` (metadata only — name, type, size; never content or the download URL) |
| `contact_include_email` | — | `False` — contact columns render the name only |
| `include_all_rows` | — | `False` — rows with no text, comments, or attachments are skipped |
| `max_rows_per_sheet` | — | `2000` — a sheet over this raises rather than truncating silently |
| `max_sheets` | — | unlimited |
| `full_sync_every` | — | `20` — force a full re-fetch of every resolved sheet every this many runs, even if its version looks unchanged; `0` disables it |
| `recheck_sheets_per_run` | — | `1` — also fully re-fetch this many sheets per run on a rotating basis, regardless of version (see "Why `version` alone isn't trusted" below); `0` disables it |
| `allow_mass_delete` | — | `False` — bypass both mass-delete guards (see below) |
| `attachment_text` | — | `False` — fetch and ingest the plain-text content of small text/CSV/Markdown attachments |
| `attachment_max_bytes` | — | `100_000` — per-attachment size cap for `attachment_text`; content beyond this is truncated, not skipped |
| `resource_name` | – | `smartsheet_documents`; give each account its own name when several sync into one dataset |

At least one of `sheet_ids`, `folder_ids`, `workspace_ids`, or `all_sheets=True` is required.

After a run, `source.cognee_sync_stats` holds counts only (sheets, rows, skipped_rows), never
sheet or row content.

## Reused from cognee

- **`DOCUMENT_SOURCE_ATTR`** / **`PIPELINE_SCOPE_ATTR`**: the connector opts into cognee's
  document-ingestion path and table-scoped sync-state cleanup the same way every other
  connector in this repo does (`cognee.tasks.ingestion.dlt_utils`).
- **`guarded_rows`**: extraction is wrapped so a host can check authorization before each row
  and before publishing the batch.

## How sync + forget-on-delete work

### Deciding whether to re-fetch a sheet

Re-fetching and re-rendering every sheet in full on every run works, but gets expensive as
your account grows. Each sheet carries a `version` number (`GET /sheets/{id}/version`,
confirmed real: developers.smartsheet.com/api/smartsheet/openapi/sheets/get-sheetversion),
and Smartsheet's own docs confirm it bumps on "add/modify cell value, add/modify
discussion/comment, add/modify row, add/remove/update version attachment, cell updated via
cell link, change formatting."

A sheet is fully re-fetched when:
- it has never been synced before,
- its `version` has changed since the last sync,
- this is a **full pass** (the first run ever, the resolved selection/options changed, or
  every `full_sync_every` runs), or
- it is picked by a small **rotating re-check** (`recheck_sheets_per_run` sheets per run,
  cycling through every known sheet over time).

Otherwise, only the cheap version check is made and nothing else happens this run.

### Why `version` alone isn't trusted

Smartsheet's documented list of what bumps `version` does **not mention row deletion** --
this was not confirmed either way during research, and testing it against a real account is
listed under "Verification notes" below. If row deletion silently does *not* bump `version`,
a deleted row could go unnoticed indefinitely with version-checking alone. The rotating
re-check and the periodic full pass (`full_sync_every`) exist specifically to catch this: even
in the worst case, every sheet gets a real, from-scratch re-fetch on a bounded schedule,
independent of whether `version` changed.

### Forget-on-delete

After a sheet is re-fetched, its current document ids (the sheet summary plus every
qualifying row) are diffed against the ids stored from its last sync. Anything missing is
tombstoned: cognee's orphan cleanup removes it from the graph. The same thing happens to a
sheet that disappears from the resolved selection entirely (deleted, unshared, or just no
longer selected) -- that is a plain diff against stored state, no extra API call needed.

**Important, confirmed limitation**: Smartsheet returns the exact same error (404,
`errorCode` 1006, `"Not Found"`) whether a sheet was genuinely deleted or the token simply
lost access to it (e.g. it was unshared). There is no API-level way to tell these two cases
apart -- confirmed via a real user report on Smartsheet's own community forum
(community.smartsheet.com/discussion/138910) that the API does not distinguish them. This
connector treats a 404 as "gone" either way, because that is the best signal available, *not*
because it is a verified deletion. The real safety net against a bulk access-loss event
being misread as mass deletion is the pair of guards below.

### Two mass-delete guards

Both can be bypassed with `allow_mass_delete=True` if the situation is genuinely intentional
(e.g. you deliberately deleted most of your sheets).

1. **Empty selection with prior state.** If the resolved selection comes back with zero
   sheets but sheets were synced before, this refuses rather than forgetting everything --
   it is far more often a selection/config mistake (an unset env var, an emptied folder) than
   every sheet having been deleted at once. An empty selection with no prior sheets (a
   legitimate first run with nothing selected yet) only logs a warning.
2. **Ratio guard.** If a pass would tombstone more than half of all previously-stored
   documents (and at least 20), it refuses. This is the real protection against the 404
   ambiguity above: a token that suddenly loses access to many sheets looks identical, at the
   API level, to those sheets having been mass-deleted.

A sheet's fetched row count is also checked against its own reported `totalRowCount` after
every full fetch; a mismatch raises rather than silently treating a partial listing as rows
having been deleted.

### Attachment text (opt-in)

By default, attachments are metadata only (name, type, size) -- set `attachment_text=True` to
also fetch and ingest the plain-text content of small `text/plain`, `text/csv`, and
Markdown attachments (by MIME type or file extension). Content over `attachment_max_bytes`
is truncated, not skipped; at most 5 attachments per row are read. The Smartsheet bearer
token is never sent to an attachment's download URL -- confirmed via the actively-maintained
SDK's own `download_attachment()`, which issues a bare, unauthenticated request to it (it is
a pre-signed storage URL, not a Smartsheet API endpoint). A 404/410 fetching an attachment's
detail or its download URL is treated as "this attachment is gone" and skipped; any other
error (403, 500, a timeout) still raises, consistent with the rest of this connector never
silently swallowing an unexpected failure.

### How a row becomes a document (the hybrid model)

Smartsheet rows are columnar — a raw cell has no meaning without its column header, and most
sheets have many rows that are purely numeric or empty. Turning every row into its own document
regardless would mostly add noise. Instead:

- **One summary document per sheet**: name, every column (with its type, and up to 10 picklist
  options), total row count, how many rows became their own documents, up to 50 top-level row
  titles, and sheet-level comments (comments not attached to any specific row).
- **One document per row, but only for rows that actually carry content**: a row qualifies if
  it has a comment, an attachment, or at least one non-system, non-excluded cell that is
  text-bearing — a picklist/multi-picklist/contact value, or free text that is not purely
  numeric/currency/percent. Checkbox, date, duration, and predecessor cells never count on
  their own. A purely numeric row (e.g. a budget line with only amounts) is skipped by default;
  set `include_all_rows=True` to keep every row regardless.
- Every rendered value carries its column header (`Status: In Progress`, never a bare value).
  Row hierarchy is written as text (`Parent: Phase 1 > Discovery`, `Children: 3 subrow(s): ...`)
  so cognee's graph extraction can connect parent/child rows across documents. Cross-sheet cell
  links and hyperlinks render as a short phrase (`linked from Budget Tracker 2026`). Formulas
  are never rendered — only their computed `displayValue`.
- Comments are capped at 20 per row (and 20 for the sheet summary), oldest first, each trimmed
  to 1000 characters.
- Attachments are **metadata only** by default — name, type, MIME type, size. The short-lived
  signed download URL is never ingested. Set `attachment_text=True` to also fetch the plain-text
  content of small text/CSV/Markdown attachments (see "How sync + forget-on-delete work").

### Privacy defaults

Smartsheet sheets often hold personal data. Safe-by-default choices:

- `contact_include_email=False` — contact columns render the person's name only.
- `include_columns` / `exclude_columns` — scope exactly which columns become content.
- System columns (auto-number, created/modified date, created/modified by) are always excluded
  from row content.
- Attachment content is never downloaded unless `attachment_text=True`, and then only for
  small text/CSV/Markdown files.

## Limitations

- **Forget-on-delete relies on an ambiguous signal.** Smartsheet's 404 does not distinguish
  "deleted" from "access revoked" (see "How sync + forget-on-delete work"); the mass-delete
  guards are the real protection, not the 404 check itself.
- Reports and dashboards are out of scope; only sheets are ingested.
- A sheet with more rows than `max_rows_per_sheet` is refused, not truncated — raise the limit
  or scope it out via `exclude_columns`/a narrower selection.
- Get Sheet's own row-paging page size (`500`, internal to this connector) is a conservative
  default, not a confirmed API maximum — see "Verification notes" below.
- Row deletion is not confirmed to bump a sheet's `version` (see "Verification notes") — the
  rotating re-check and periodic full pass exist specifically to cover this gap.

## Verification notes (read before relying on this in production)

Most of this connector's API-mechanics decisions were verified directly against the actively
maintained [`smartsheet/smartsheet-python-sdk`](https://github.com/smartsheet/smartsheet-python-sdk)
source (commit `a554eaf`, 2026-10-12) and official docs at developers.smartsheet.com, not
guessed. Two things specifically worth testing against a real account before depending on them:

- **Get Sheet's row-paging page size.** The maximum accepted `pageSize` for `GET
  /sheets/{id}`'s own row pagination was not found in research; `500` is a conservative
  internal default, not a confirmed ceiling.
- **The full `exclude` and `level` query parameters on Get Sheet** are not used by this
  connector (every column is fetched; no column-type verbosity downgrade is requested), so
  their exact behavior was not load-bearing for this phase and was not deeply verified.
- **UNVERIFIED: does row deletion bump a sheet's `version`?** Smartsheet's documented list of
  version-bump triggers ("add/modify cell value, add/modify discussion/comment, add/modify
  row, add/remove/update version attachment, cell updated via cell link, change formatting")
  does not mention row *deletion* either way. To check live: note a sheet's `version` via
  `GET /sheets/{id}/version`, delete a row through the Smartsheet UI, and check `version`
  again. If it does not change, this connector's rotating re-check and periodic full pass are
  the only things catching that deletion -- tune `recheck_sheets_per_run` / `full_sync_every`
  accordingly for sheets where rows are deleted often.
- **UNVERIFIED: does a 403 ever mask as a 404?** This connector assumes 403 ("You are not
  authorized...", `errorCode` 1004) means "exists but inaccessible" and 404 ("Not Found",
  `errorCode` 1006) means "gone" -- and only treats 404 as a forget-on-delete signal. It was
  not confirmed whether Smartsheet ever returns 404 instead of 403 for an unshared-but-extant
  sheet (which would make the 404-ambiguity described above even broader). To check live:
  share a sheet with a second token, sync it, then remove that token's access (without
  deleting the sheet) and sync again -- confirm the status code is 403, not 404.
- **UNVERIFIED: does an attachment's pre-signed download URL reject an Authorization header,
  or just ignore it?** This connector never sends one regardless (confirmed safe-by-default
  via the SDK's own unauthenticated `download_attachment()`), so this only matters if you were
  relying on sending one for some other reason.

## Testing

```bash
uv run pytest tests/
```

No Smartsheet account or API token is needed. `tests/fake_smartsheet.py` is an in-memory
Smartsheet API modeling both of Smartsheet's current pagination contracts: legacy
`page`/`pageSize` (List Sheets, the discussions listing, Get Sheet's own row-paging) and cursor
`lastKey`/`maxItems` (the `/workspaces/{id}/children` and `/folders/{id}/children` endpoints),
plus the version, attachment-detail, and pre-signed-download-URL endpoints used by Phase 2.

- `test_smartsheet_source.py` covers factory validation, host/region normalization, and the
  resource markers.
- `test_smartsheet_sync.py` covers the HTTP client (retries, rate limiting, both pagination
  styles), selection resolution, cell rendering, row inclusion, and the full-snapshot build.
- `test_smartsheet_ingest.py` runs a real `dlt.pipeline` into sqlite: initial sync, an
  idempotent re-run, and failure propagation on an API error.
- `test_smartsheet_forget.py` covers incremental sync (version checks, the rotating
  re-check, periodic full passes), forget-on-delete (row- and sheet-level tombstoning, a
  sheet's 404 vs. 403 vs. moving out of scope), both mass-delete guards, the row-count
  integrity check, state surviving mid-run failures untouched, attachment text, and two
  end-to-end tests: a real `dlt.pipeline` physically removing a tombstoned row, and a deleted
  row actually disappearing from cognee's graph (not just the dlt table).
