# Jira Service Management connector

Sync selected JSM Cloud service desks into Cognee as request documents, including
descriptions, status, comments, and SLA state. Uses Atlassian email/API-token
Basic authentication, matching the Confluence connector. All upstream calls are GETs.

## Setup

From this directory, install the local package (Python 3.11–3.13):

```sh
python -m pip install -e '.[test]'
```

Create an [Atlassian API token](https://id.atlassian.com/manage-profile/security/api-tokens).
Use a JSM agent account with Browse Projects, access to the selected requests,
and permission to read comments and SLA metrics. Customer-only credentials do
not provide the project-wide search/SLA access this connector needs. This
connector targets Cloud site URLs and unscoped API tokens; scoped gateway tokens,
OAuth, and Data Center are not supported.

Set these environment variables in your shell (PowerShell example):

```powershell
$env:JSM_URL = 'https://your-domain.atlassian.net'
$env:JSM_EMAIL = 'you@example.com'
$env:JSM_API_TOKEN = '<your-token>'
$env:LLM_API_KEY = '<your-llm-key>'
python examples/example.py
```

The first invocation lists accessible service desk IDs without ingesting anything.
Choose desks, then run again:

```powershell
$env:JSM_SERVICE_DESK_IDS = '10,20'
$env:JSM_DATASET = 'jsm_requests'
python examples/example.py
```

The example waits for ingestion/cognify before searching. It preserves the dataset
and cursor across runs. It never prunes existing data.

## Python API

```python
from cognee_community_connector_jira_service_management import jira_service_management_source

source = jira_service_management_source(
    base_url="https://your-domain.atlassian.net",
    email="you@example.com",
    api_token="...",
    service_desk_ids=["10"],
    include_comments=True,
    include_internal_comments=False,
    include_sla=True,
)
await cognee.remember(
    source,
    dataset_name="jsm_requests",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
    run_in_background=False,
)
```

`list_service_desks` accepts the same connection arguments. The source requires
an explicit nonempty desk selection. Internal or unknown-visibility comments are
excluded by default; opt in only for a dataset whose readers may see agent notes.
Request content is processed by your configured embedding/LLM providers.

## Sync semantics

- Each request becomes one document with its stable numeric issue ID, source URL,
  title, description, status, ordered comments, and readable SLA state. SLA lines
  report ongoing/paused/completed status and breach state, excluding ticking
  durations, timestamps, and API links so clock changes do not cause re-ingestion.
  The source opts into
  Cognee document mode, so prose goes through entity extraction.
- Each selected desk is resolved to its project. Enhanced
  [`/rest/api/3/search/jql`](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/)
  searches only issues with a request type. The field is resolved via
  `/rest/api/3/field` using schema type `com.atlassian.servicedesk:vp-origin`, then
  referenced as `cf[<id>]` so renamed or translated labels work. Missing or
  ambiguous field metadata aborts the sync instead of silently returning no rows.
  It follows `nextPageToken`;
  comments, SLA metrics, and desk discovery follow JSM offset pagination.
- A persisted `updated` cursor uses the sync start time and a five-minute overlap.
  Unquoted numeric epoch milliseconds are supported by Atlassian's
  [Updated field documentation](https://support.atlassian.com/jira-software-cloud/docs/jql-fields/#Updated)
  and avoid account-timezone ambiguity. Live repeated-sync validation passed.
  Overlapping results
  are re-read and content hashes suppress unchanged upserts. Newly visible old
  requests are fetched regardless of cursor. Active SLA requests are refreshed
  on every run even without an issue update.
- Every run also scans all in-scope IDs. Missing known requests are checked
  directly: a still-readable request in a selected desk is retained, a 404 or
  a move outside the selected desks emits a dlt hard-delete marker. This includes
  deletion of the final request. Jira can return 404 for lost visibility as well
  as deletion; such content is forgotten too. A 401/403 or failed/incomplete
  listing aborts without advancing sync state. Transient HTTP failures retry
  with backoff and respect `Retry-After`.
- Cognee reconciles the merged staging table through its existing orphan cleanup.
  Always pass `max_rows_per_table=0` so reconciliation sees the entire corpus.
  Use a separate dataset per account/site/selection. Changing the connection or
  content options within an existing resource state raises an error: use a new
  dataset and explicitly retire the old one if needed.

Search is eventually consistent: updates delayed beyond the five-minute overlap
can be missed. A new dedicated dataset performs a full backfill. The ID sweep is
linear in the number of requests; it is necessary because JQL updated cursors do
not expose deleted issues. Do not run overlapping syncs against the same dataset.
Comment changes rely on Jira updating the parent issue timestamp. SLA clocks are
refreshed only while an ongoing cycle was observed or an issue changes.

## Verification

```sh
python -m pytest tests -q
python -m ruff check .
```

Tests use mocked HTTP and a real dlt SQLite destination: pagination, comment
visibility, incremental overlap, unchanged rows, edits, active SLA refresh,
search omissions, failure rollback, and deletion through an empty final table.
Regression cases check that SLA clock ticks leave content unchanged while
breach/lifecycle changes update it. A separate test exercises the installed
Cognee document-source recognition and row-to-document mapping. These tests do
not claim live Atlassian or graph/vector end-to-end coverage.

Before submitting, run the example against a synthetic JSM project: ingest two
requests, edit/add a public comment, verify an internal note is absent, delete
one request and then the final request, and inspect Cognee's Data, graph, and
vector stores after each sync. Confirm deleted content is no longer searchable.

API reference: [JSM request, comments, and SLA endpoints](https://developer.atlassian.com/cloud/jira/service-desk/rest/api-group-request/).
