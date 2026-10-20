# cognee-community-connector-logrocket

A LogRocket data-source connector for [cognee](https://github.com/topoteretes/cognee):
ingest session metadata and issue reports into searchable memory.

It exposes a `dlt` source that renders records as normal documents through Cognee's
document-mode ingestion path. Session replay payloads, DOM recordings, request bodies
and HAR data are not ingested.

## Requirements

Python `>=3.11,<3.14`, a LogRocket API key with access to the selected project, and
Cognee LLM/embedding configuration. The package pins `cognee==1.4.0` and requires
DLT `>=1.31.0,<2` for its merge-safety checks.

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
but does not guarantee detection of every update. Session date filtering is expressed
in a natural-language query, not a structured date-filter contract.

Use a dedicated dataset for each project and resource selection. `remember_logrocket`
enforces `write_disposition="merge"` and `primary_key="id"`; direct Cognee calls must
pass both explicitly. `replace` and `append` are rejected before requests are made.
Do not reuse the dataset for another project or change its resource selection.

See [API_INTEGRATION.md](API_INTEGRATION.md) for extending synchronization when
supported retrieval, update and deletion APIs become available.

## Setup

1. Create a key under LogRocket **Settings → API Keys**, and note your organization
   and project IDs. The connector uses the official project-scoped MCP endpoint.
2. Set `LOGROCKET_API_KEY`, `LOGROCKET_ORGANIZATION` and `LOGROCKET_PROJECT`, plus
   your Cognee LLM/embedding settings. Run the example with:

   ```bash
   uv run --env-file .env python examples/ingest_logrocket.py
   ```

   `.env` is local and ignored; it is loaded only when explicitly requested.
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
