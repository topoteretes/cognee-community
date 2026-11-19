# cognee-community-connector-circleci

A CircleCI data-source connector for **cognee**: sync your CI/CD pipeline
execution history into memory — *"ask my builds"*.

It exposes a **dlt source** you hand to `cognee.remember(...)` /
`cognee.add(...)`. Pipeline executions are rendered as structured documents
and ingested as **normal documents** (they flow through cognee's cognify
entity-extraction pipeline, not the deterministic dlt-row path), via cognee's
document-mode marker.

## Requirements

This connector requires a cognee release that ships *"document-mode"* — i.e.
one that exports `DOCUMENT_SOURCE_ATTR` from
`cognee.tasks.ingestion.dlt_utils` and whose `resolve_dlt_sources` route
respects it. That capability first shipped in **cognee 1.4.0**.

## Installation

```bash
pip install "cognee[circleci] @ git+https://github.com/topoteretes/cognee-community.git#subdirectory=packages/connector/circleci"
```

Or, from a local checkout of `cognee-community`:

```bash
pip install -e packages/connector/circleci
```

## Authentication

Create a **Personal API Token** in CircleCI (User Settings → Personal API Tokens).
The connector uses the `Circle-Token` HTTP header for authentication.
Provide the token either as an argument or via the `CIRCLECI_API_TOKEN`
environment variable:

```bash
export CIRCLECI_API_TOKEN="your-personal-api-token-here"
```

## Usage

```python
import cognee
from cognee_community_connector_circleci import circleci_source

# Configure cognee first (cognee config, infrastructure, etc.)

# Ingest pipeline executions from specific projects
source = circleci_source(
    project_slugs=["gh/your-org/your-repo", "gh/your-org/another-repo"],
    branch="main",  # optional
)
await cognee.add(source)
```

After ingestion you can query your CI memory:

```python
results = await cognee.search("Which pipelines failed in the last 24 hours and why?")
```

## Sync model: replace per pipeline with in-progress re-check

This connector uses ``write_disposition="replace"`` per pipeline, so each
sync produces the complete current set of that pipeline's executions.

### Incremental + in-progress re-check

1. Uses a ``created_at`` cursor stored in dlt state, with a 5-minute overlap
   window to catch late-updating jobs.
2. Pipelines still running at sync time are stored in dlt state and
   **re-checked individually** on the next run so their final status lands.

### Safety guarantee

> An API error **aborts the run before staging is replaced**. A partial
> snapshot must never drive mass deletions. Transient errors (429, 5xx) are
> retried with exponential backoff honoring the ``Retry-After`` header;
> permanent errors (401 invalid token) raise immediately. Individual job
> detail fetch failures are logged as warnings and skipped — the pipeline
> execution is still discoverable by its metadata.

### Edge cases documented

- **Retention expiry**: Expired pipelines disappear from the API listing but
  aren't confirmed deletions. This is treated as a soft scenario; cognee
  forgetting only triggers when we have a positive deletion signal (or the
  user configures an explicit retention window).
- **Failure output scope**: The connector fetches structured failure data
  from the tests endpoint (name, file, assertion message). Step-level logs
  via ``output_url`` are intentionally excluded by default to keep the
  connector focused on *signal over noise*. They can be enabled optionally
  with size limits.
- **Still-running pipelines**: Pipelines in ``running`` or ``on_hold`` state
  at sync time are stored for re-check on the next run. Their partial data
  is still ingested so the pipeline is discoverable.
- **Multi-project slugs**: When multiple ``project_slugs`` are provided,
  each project is synced independently. A failure in one project does not
  affect others — the error propagates after successful projects have been
  processed.
- **Rate limits**: CircleCI's API enforces rate limits per token. The
  connector honors ``Retry-After`` headers and uses exponential backoff to
  stay within limits.

## Data model (document-mode)

One document per **pipeline execution** containing:
- Pipeline / workflow / job status, timestamps, commit / branch context
- For **failed jobs**: bounded failure output from the tests endpoint
  (structured failure data — *not* full build logs)
- For **successful jobs**: status + metadata only (no logs), keeping the
  connector focused on *signal over noise*

## Acceptance criteria

- [x] A user connects via API token and selects what to ingest
- [x] Selected content is ingested and searchable in cognee
- [x] Incremental sync picks up only what changed since the last run
- [x] Deleting the source upstream removes it from the graph on the next sync
- [x] README with setup steps and a runnable example under `examples/`
- [x] Tests covering the ingest path and the incremental cursor

## Layout

```
packages/connector/circleci/
├── README.md
├── cognee_community_connector_circleci/
│   ├── __init__.py
│   └── circleci.py
├── examples/
│   └── example.py
├── tests/
│   ├── fixtures/           # Recorded API responses for offline tests
│   └── test_circleci.py
└── pyproject.toml
```

## Reference implementation

Read `packages/connector/notion/` first — it is the closest working
reference for the dlt + document-mode pattern this connector follows.
The sync model and edge-case documentation draw from lessons learned
reviewing high-quality Mergetober submissions.
