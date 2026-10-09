# Airtable connector

Bring an Airtable base's records, table schema, and record comments into Cognee memory.
The connector performs **full source reconciliation with incremental document emission**:
it reads the complete selected source on each sync, emits only changed documents, and
removes documents confirmed deleted upstream. It issues read requests only.

## Install and configure

The package requires Python 3.11–3.13. It pins Cognee 1.6.3 and supports
`dlt[sqlalchemy]>=1.9.0,<1.31`; the development lockfile fixes dlt at 1.30.0.
Install the built wheel, or run from this directory:

```bash
uv sync --frozen --all-extras --python 3.12
```

1. Create a [personal access token](https://airtable.com/create/tokens) with access to
   the chosen base and scopes `data.records:read` and `schema.bases:read`.
   Comments are enabled by default and also require `data.recordComments:read`.
2. Find the base ID (`app...`) and, if selecting individual tables, their IDs (`tbl...`).
   Use IDs rather than display names so renaming a table does not change its identity.
3. Add a **Last modified time** field to every selected table. Configure it to track
   **All editable fields** and **include time**. The default field name is
   `Last modified time`; a custom field name or ID can be supplied below.
4. Export the token and base ID. Configure Cognee's LLM and embedding credentials
   as described in the [Cognee documentation](https://docs.cognee.ai).

```bash
export AIRTABLE_ACCESS_TOKEN="your-personal-access-token"
export AIRTABLE_BASE_ID="appYourBaseId"
# Optional comma-separated table IDs; omitted means all tables in this base.
export AIRTABLE_TABLE_IDS="tblCustomers,tblProjects"
uv run --frozen python examples/example.py
```

The example stores data in `airtable_<base ID>` without pruning existing memory and
uses dataset-scoped `recall()` to return matching passages. Re-run it to synchronize
changes and deletions. Graph extraction and embedding need the configured model
providers; `CHUNKS` retrieval does not ask an LLM to generate an answer.

## Use the source

```python
import cognee
from cognee_community_connector_airtable import airtable_source

source = airtable_source(
    base_id="appYourBaseId",
    table_ids=["tblCustomers", "tblProjects"],  # None selects all current tables
    last_modified_field={
        "tblCustomers": "Last modified time",
        "tblProjects": "fldCustomModifiedTime",
    },
    include_schema=True,
    include_comments=True,
)
result = await cognee.remember(
    source,
    dataset_name="airtable_business",
    self_improvement=False,
    dlt_config={
        "primary_key": "id",
        "write_disposition": "merge",
        "max_rows_per_table": 0,
    },
)
passages = await cognee.recall(
    "What do we know about current customer projects?",
    datasets=["airtable_business"],
    query_type=cognee.SearchType.CHUNKS,
)
for passage in passages:
    print(passage.text)
```

`airtable_source()` accepts keyword-only configuration:

| Argument | Default | Meaning |
|---|---|---|
| `base_id` | Required | Exactly one base to reconcile |
| `table_ids` | `None` | Selected table IDs; omitted selects all tables |
| `token` | Environment | PAT; otherwise read `AIRTABLE_ACCESS_TOKEN` |
| `last_modified_field` | `"Last modified time"` | Common field name/ID, or mapping of table IDs to field names/IDs |
| `include_schema` | `True` | Emit table-schema documents |
| `include_comments` | `True` | Include each record's comments in its document |
| `session` | Managed HTTP session | Injectable session with a `get()` method, mainly for testing |

The returned `DltResource` carries Cognee's document-source and pipeline-scope markers
on the exact object passed to `remember()`. Keep `merge` and the unlimited retained-table
read (`max_rows_per_table=0`) in `dlt_config`: Cognee's default `replace` would discard
unchanged staged documents. Base-scoped document IDs and Cognee's dataset-scoped staging
keep separate bases and destination datasets isolated.

## What a sync means

- Each record becomes one document, with field names resolved from schema metadata and
  optional embedded comments. Each enabled table schema becomes a separate document.
- Complete metadata, record, and enabled comment reads finish before any delta is emitted.
  A missing permission, malformed response, exhausted retry, or incomplete page aborts
  the sync. Existing content and connector state are preserved.
- A successful empty table deletes its previously known records. A complete schema
  inventory confirming a previously synchronized table has disappeared deletes both its
  schema and every known record, provided that table is still in the selected scope.
- Deselecting a table preserves its existing content. Use a new dataset for a changed
  selection, or explicitly remove old content with Cognee's supported deletion tools.
  An unknown selected table ID on first use is a configuration error.
- Disabling comments updates affected record documents to remove comments. Disabling
  schema ingestion removes previously ingested schema documents for selected tables.
  `include_schema=False` still fetches metadata and requires `schema.bases:read` for cursor
  validation, field-name resolution, and table-disappearance detection.
- Per-table watermarks, known IDs, and canonical content fingerprints are persisted in
  dlt state. A fresh source factory with the same base and dataset reuses that state.
  Restored records and equal, blank, older, or backwards-moving timestamps are reconciled
  through complete content comparison.
- Emitted rows contain only `id`, `title`, `content`, a stable Airtable `url`, and the
  hard-delete marker. Field and comment ordering is deterministic. Cursor values and
  expiring attachment/thumbnail URLs are excluded from the documents themselves.
  Attachments contribute stable IDs, filenames, types, sizes, and dimensions; bytes are not downloaded.
- A downstream failure after staging is retried by re-running the same foreground sync.
  Cognee reads retained staging even when no new source documents are emitted, then
  performs scoped cleanup after successful ingestion. Inspect a failed `remember()` result
  and retry; a completed dlt load alone is not proof that graph/vector work completed.
  Cognee 1.6.3 logs orphan-cleanup failures and can still return a completed result;
  inspect cleanup errors and retry the same sync to remove content retained after such a failure.

The connector checks that the cursor field is a valid `lastModifiedTime` field returning
a date and time. The published metadata does not conclusively expose whether the field
tracks **All editable fields**, so that setting must be checked in Airtable's UI.
[Airtable explains](https://support.airtable.com/articles/3423064340-last-modified-time-and-last-modified-by-fields-in-airtable)
that computed-field changes may not advance this field and some values can be blank.
Complete reconciliation catches those changes without depending solely on the watermark.

## Request cost

Every sync reads the selected records and enabled comments, including unchanged runs.
Approximate calls are one base-schema request, record pages of up to 100 records per
table, and comment pages for every record. For one table with 300 records and at most
one comment page per record, this is about **304 requests** with comments and **4** without.
Select only needed tables or set `include_comments=False` to reduce polling cost.

Airtable documents five requests per second per base and 1,000 monthly calls per
Free-plan workspace. Metadata requests count toward the same quota. The connector
paces requests, uses bounded retries, and waits at least thirty seconds after a 429;
an exhausted monthly quota still fails the sync. See
[Airtable's current API limits](https://support.airtable.com/articles/7735693959-managing-api-call-limits-in-airtable).

## Verification

```bash
uv sync --frozen --all-extras --python 3.12
uv run --frozen pytest tests/unit -q
uv run --frozen pytest tests/integration -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv build
```

Unit tests cover source configuration, fetching, reconciliation, and canonical rows.
Integration tests use real local SQLite staging/metadata, Ladybug graphs, and LanceDB
vectors with mocked Airtable HTTP and deterministic model responses. They exercise
retrieval, replacement, deletion, isolation, and recovery. Required tests do not skip
when dependencies are missing. The repository's Airtable workflow runs unit tests on
Python 3.11/3.12 and integration plus installed-wheel ingestion on Python 3.12, with no
external credentials.

For a wheel smoke check outside the checkout:

```bash
uv export --frozen --no-dev --no-emit-project --no-hashes --output-file /tmp/airtable-constraints.txt
uv venv --python 3.12 /tmp/airtable-wheel
uv pip install --python /tmp/airtable-wheel/bin/python --constraint /tmp/airtable-constraints.txt dist/*.whl
cp examples/offline_smoke.py /tmp/airtable-wheel-smoke.py
cd /tmp
/tmp/airtable-wheel/bin/python /tmp/airtable-wheel-smoke.py
```

The smoke script uses the installed package, injected HTTP fixtures, temporary storage,
and real Cognee document ingestion. It does not exercise model quality or a live base.

For optional live acceptance, run the example against a disposable base, then edit a
distinctive field/comment fact, remove a record, empty a table, and delete an entire
selected table. Re-run after each change and check that new facts are retrievable and
old facts disappear while other selected content remains. Also try an unchanged sync
and `include_comments=False`. Report live Airtable results separately from local fixture
tests and hosted CI; no live behavior is implied by offline passes.
