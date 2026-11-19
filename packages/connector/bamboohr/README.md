# cognee-community-connector-bamboohr

A BambooHR data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your employee directory and company files (policies, handbooks, ...) into memory,
"ask my HR system".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Employees and files are
ingested as **normal documents** through cognee's document-mode marker, so they flow
through cognify entity extraction.

## Install

```bash
cd packages/connector/bamboohr && uv sync
```

### cognee version

`pyproject.toml` pins `cognee==1.4.0`, the first release with document mode, matching the
other document-mode connectors (Notion, Google Drive) so they can be installed together.
The tests also pass on cognee 1.6.3 (the latest release at the time of writing). Bumping
the pin is a one-line change if all connectors move to a newer cognee.

## Setup

1. In BambooHR, click your name (lower left) → **API Keys** → add a key. The connector
   only sends `GET` requests. An API key has the same permissions as the user who
   created it, so consider creating it under a BambooHR user that can only see the
   employees and file categories you want in memory.
2. Set your connection details (or pass `company_domain=` / `api_key=`):

   ```bash
   export BAMBOOHR_COMPANY_DOMAIN=acme   # from https://acme.bamboohr.com
   export BAMBOOHR_API_KEY=...
   ```

3. Configure cognee's LLM and embeddings in `.env` like any other cognee run. With small
   local Ollama models (e.g. `llama3.1:8b`), also set
   `LLM_INSTRUCTOR_MODE="json_schema_mode"`: in the default JSON mode these models can
   return the schema instead of the data, which makes cognify fail.

## Usage

```python
import cognee
from cognee_community_connector_bamboohr import bamboohr_source

await cognee.remember(
    bamboohr_source(),
    dataset_name="bamboohr",
    primary_key="id",
    write_disposition="merge",  # required, see below
)

answer = await cognee.search(
    query_text="Who works in the Sales department?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["bamboohr"],
)
```

`write_disposition="merge"` is required. cognee defaults to `"replace"`, which would drop
every employee that did not change since the last run.

See `examples/example.py` for the full flow (backfill, search, incremental re-sync).

### Choosing what to ingest

| Option | Default | Effect |
|---|---|---|
| `fields` | `DEFAULT_EMPLOYEE_FIELDS` | Employee fields to ingest (the allowlist) |
| `include_inactive` | `False` | Keep terminated employees instead of forgetting them |
| `include_files` | `True` | Also sync company files; set `False` if your API key can't read files |
| `file_categories` | `None` (all) | Only sync files in these categories, e.g. `["Company Files"]` |

## Employee data is sensitive

BambooHR's get-employee endpoint returns only `id` unless fields are named, so the
connector only ever requests the fields in its allowlist. The default is:

```
firstName, lastName, preferredName, jobTitle, department, division,
location, workEmail, supervisor, status, hireDate
```

SSN, date of birth, pay, home address, personal contact details, gender and ethnicity are
deliberately left out. Pass `fields=[...]` to choose your own. `status` is always
requested (to detect terminated employees) but is only written into memory if it is in
your list.

Company files are ingested as they are. A file can hold personal data too (a benefits
export, for example, may list every employee), so use `file_categories` to pick only the
categories you want, or `include_files=False`.

## How sync and forget-on-delete work

Both tables use `write_disposition="merge"` with a `_deleted` hard-delete column: a row
yielded with `_deleted=True` is removed from staging, and cognee's `orphan_cleanup` then
forgets it from the graph and vector stores.

**Employees (incremental).** Each run calls `GET /employees/changed?since=<cursor>`. The
first run uses `1970-01-01T00:00:00Z`, so the backfill and later runs share one code path.

- `Inserted` / `Updated` → the employee is fetched with the allowlisted fields and upserted.
- `Deleted`, a 404, or (by default) `status: Inactive` → a `_deleted` marker.
- The response's `latest` timestamp becomes the next cursor. It is saved only after every
  change has been yielded, so a run that fails midway retries the same window next time.
- BambooHR's `since` is inclusive, so the most recently changed employee is fetched again
  on the next run. `merge` makes this harmless, and it means a change made in the same
  second as the cursor is never skipped.

**Company files (snapshot).** BambooHR's files API has no change feed and no modified
date, so each run lists every file (`GET /files/view`) and downloads the readable ones.
Unchanged files keep the same content hash, so cognee does not re-cognify them.
Deletions are found by comparing this run's file ids with the previous run's.

- PDF (via `pypdf`) and plain text (`.txt`, `.md`, `.csv`) are read; other types are skipped.
- A file that can no longer be downloaded (403/404) is forgotten.
- A file that fails to parse is skipped with a warning and its earlier copy is kept.
- If the listing itself fails, the run aborts, so a partial listing is never mistaken
  for deletions.
- Narrowing `file_categories` forgets files from categories that are no longer selected.

## Testing

```bash
uv run --with pytest pytest tests/
```

The tests mock the BambooHR API (no account needed) and cover the HTTP retry layer, the
field allowlist, the incremental cursor, delete/inactive markers, company-file parsing
and deletion, and a real `dlt` merge into SQLite that shows a deleted employee being
removed.
