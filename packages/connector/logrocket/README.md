# cognee-community-connector-logrocket

A LogRocket data-source connector for [cognee](https://github.com/topoteretes/cognee):
ingest session metadata and issue reports into searchable memory.

It exposes a `dlt` source that renders records as normal documents through Cognee's
document-mode ingestion path. Session replay payloads, DOM recordings, request bodies
and HAR data are not ingested.

## Requirements

Python `>=3.11,<3.14`, a LogRocket API key with access to the selected project, and
Cognee LLM/embedding configuration. Requires `cognee>=1.6.3,<2` and
DLT `>=1.30,<1.31`. The DLT floor preserves destination fingerprinting for
ownership checks while staying within Cognee's compatible range.

## Install

From this monorepo:

```bash
cd packages/connector/logrocket
uv sync --locked
```

## Usage

```python
import cognee
from cognee_community_connector_logrocket import logrocket_source, remember_logrocket

source = logrocket_source(
    organization_id="YOUR_ORG",
    project_id="YOUR_PROJECT",
    resources=("sessions", "issues"),
    acknowledge_bounded_results=True,
)
await remember_logrocket(source, dataset_name="logrocket_my_project")

answer = await cognee.search(
    query_text="What errors affect checkout sessions?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["logrocket_my_project"],
)
```

Select `sessions`, `issues`, or both. Defaults re-fetch seven days of sessions and
30 days of issue occurrences; configure `session_lookback_days` and
`issue_lookback_days`, or supply timezone-aware `window_start` and `window_end`.
Optional `session_query` and `issue_query` further restrict searches.

See [examples/ingest_logrocket.py](examples/ingest_logrocket.py) for the full flow.

The public API is `logrocket_source` and `remember_logrocket`. Read-only development
diagnostics live in `examples/diagnostics.py`, outside the installed connector. From
the package checkout, run `uv run python examples/inspect_capabilities.py --help`.
This replaces the previous `cognee-logrocket` console command.

Hosts can pass a synchronous `check_active` callback to stop extraction when access
is revoked. It is checked before extraction and publishing rows, and before each
built-in MCP request/retry. Callback failures propagate; they are not retried as
transport errors. Injected clients must implement their own in-request checkpoints.

`source.cognee_sync_stats` contains counters keyed by `sessions` and `issues`.
Each resource also exposes its own counter dictionary. Counters reset when that
resource starts extraction: `scanned`, `emitted`, `skipped` (exact duplicates),
`deleted` (always zero), `failed`, `requests` (logical search calls, excluding
transport retries/discovery), and `request_complete`. The last flag means only
that extraction of that resource's returned response finished; it does not mean
exhaustive retrieval, committed loading, or successful cognification. Unselected
resources are not run. Read each resource's latest counters separately.

The connector uses Cognee's shared pipeline-scope marker and guarded-row helper.
The ownership registry remains necessary: pipeline names alone do not prevent
two different projects from targeting the same staging dataset.

## How sync and deletion work

Each run requests the configured window and merges returned records by ID. Session
IDs use the recording/session pair, scoped to the organization and project. Issue
IDs use a provisional hash of the source URL because permanent issue IDs have not
been confirmed. Unchanged content retains its Cognee identity.

LogRocket support confirmed that MCP has no exhaustive pagination, reliable change
detection, or deletion discovery. Searches may omit records even below their limit.
`acknowledge_bounded_results=True` acknowledges these search limitations. Results
reaching the configured cap (maximum 100) or malformed records abort extraction;
narrow the window or query before retrying a capped result.

There is no persisted sync watermark or automatic forget-on-delete. Missing results
never delete stored documents. Re-fetching issues can refresh returned older reports,
but does not guarantee detection of every update. Session dates are checked locally
against the requested UTC interval (inclusive start, exclusive end); an out-of-window
result aborts extraction. Exact duplicate documents are collapsed; conflicting versions
of one ID abort extraction instead of allowing an arbitrary version to win.

Use a dedicated dataset for each project and resource selection. `remember_logrocket`
enforces `write_disposition="merge"` and `primary_key="id"`; direct Cognee calls must
pass both explicitly. `replace` and `append` are rejected before requests are made.
The first extraction reserves the dataset for its project and resource selection;
subsequent mismatches fail before searching. Ownership is recorded in DLT state and
`logrocket_ownership.sqlite3` in the DLT pipelines directory, outside the pipeline
folders DLT may reset. Preserve both with your staging data. A failed first import
retains its reservation. Existing datasets acquire a binding on their next extraction;
use their original project and resource selection when upgrading.
When `dataset_name` is omitted, the helper selects a deterministic name derived from
the organization, project and resource selection.

See [future API integration guide](examples/API_INTEGRATION.md) for extending synchronization when
supported retrieval, update and deletion APIs become available.

## Setup

1. Create a key under LogRocket **Settings → API Keys**, and note your organization
   and project IDs. The connector uses the official project-scoped MCP endpoint.
2. Set `LOGROCKET_API_KEY`, `LOGROCKET_ORGANIZATION` and `LOGROCKET_PROJECT`, plus
   your Cognee LLM/embedding settings. Run the example with:

   ```bash
   uv run --env-file .env python examples/ingest_logrocket.py
   ```

   `.env` is local and ignored. Cognee may also discover and load a project `.env`
   during import. For environment-only configuration, set `PYTHON_DOTENV_DISABLED=1`
   before starting Python; the connector itself does not parse dotenv files.
3. Alternatively configure the key in local `.dlt/secrets.toml`:

   ```toml
   [sources.logrocket]
   api_key = "YOUR_LOGROCKET_API_KEY"
   ```

   Resolution order: explicit `api_key`, `LOGROCKET_API_KEY`, then DLT's
   `sources.logrocket.api_key` providers (including `SOURCES__LOGROCKET__API_KEY`).
   Credentials and local DLT configuration must not be committed.

User identity and location are excluded by default. `include_user_id=True` and
`include_location=True` opt into user IDs and coarse location. Names, emails and IP
fields remain excluded; free text redacts common emails, IPv4 addresses and tokens.
Review your data and privacy requirements before ingesting production records.
Provenance URLs containing credentials, recognized sensitive query parameters or
redactable text are rejected rather than rewritten, preserving URL-based issue identity.
Invalid numeric values are rejected. Transient requests respect `Retry-After` within
the operation deadline; permanent errors, including wrapped authentication failures,
are not retried.

## Testing

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Tests cover parsers, deterministic IDs, credentials, retries, schema changes, result
limits, SQLite merges and preservation of omitted records. No LogRocket key is needed.
GitHub Actions is configured for Python 3.11–3.13 and the declared DLT minimum.

An optional local model test uses synthetic source records with Ollama:

```bash
ollama pull mistral:7b-instruct
ollama pull all-minilm
LOGROCKET_RUN_COGNEE_E2E=1 uv run --env-file examples/ollama.env.example \
  pytest tests/test_cognee_integration.py::test_real_cognee_remember_and_search -q
```

Ollama must be running at `127.0.0.1:11434`. The profile also works with the ingestion
example by adding `--env-file examples/ollama.env.example` alongside `--env-file .env`.
