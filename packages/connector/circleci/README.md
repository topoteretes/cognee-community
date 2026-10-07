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

## Data model (document-mode)

One document per **pipeline execution** containing:

- Pipeline / workflow / job status, timestamps, commit / branch context
- For **failed jobs**: bounded failure output from the tests endpoint
  (structured failure data — *not* full build logs)
- For **successful jobs**: status + metadata only (no logs), keeping the
  connector focused on *signal over noise*

## Incremental sync

The connector uses a `created_at` cursor stored in dlt state, with a small
overlap window (default 5 minutes) to catch late-updating jobs. Pipelines
still running at sync time are stored in dlt state and re-checked on the
next run so their final status lands.

## Deletion / retention

`write_disposition="replace"` is used per pipeline, so each sync produces
the complete current set of that pipeline's executions. Anything dropped
from the API listing gets cleaned up via cognee's orphan cleanup.

**Retention expiry** is treated as a *soft* scenario: expired pipelines
disappear from the API listing but aren't confirmed deletions. This is
documented as a known limitation; actual cognee forgetting only triggers
when we have a positive deletion signal (or the user configures an
explicit retention window).

## On the open questions

1. **Failure output scope**: Start with the tests endpoint (structured
   failure data). For step-level logs, v1.1's `output_url` is useful but
   produces large/unstructured content — it is exposed as an *optional*,
   explicitly-enabled feature with size limits rather than default-on.
2. **Retention expiry**: See *Deletion / retention* above.

## Acceptance criteria

- [ ] A user connects via API token and selects what to ingest
- [ ] Selected content is ingested and searchable in cognee
- [ ] Incremental sync picks up only what changed since the last run
- [ ] Deleting the source upstream removes it from the graph on the next sync
- [ ] README with setup steps and a runnable example under `examples/`
- [ ] Tests covering the ingest path and the incremental cursor

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
│   └── test_circleci.py
└── pyproject.toml
```

## Reference implementation

Read `packages/connector/notion/` first — it is the closest working
reference for the dlt + document-mode pattern this connector follows.
