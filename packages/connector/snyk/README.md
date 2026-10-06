# cognee-community-connector-snyk

A Snyk data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Snyk issues into memory — "ask my vulnerabilities".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Snyk
issues are rendered to text and ingested as **normal documents** (they flow through
cognee's cognify entity-extraction pipeline, not the deterministic dlt-row path), via
cognee's document-mode marker.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it. **This is not in cognee 1.3.0.** The `cognee==` pin in
> `pyproject.toml` is set to the first release that includes document-mode.

## Install

```bash
uv pip install cognee-community-connector-snyk
# or, from this monorepo:
cd packages/connector/snyk && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_snyk import snyk_source

await cognee.remember(
    snyk_source(),  # SNYK_TOKEN + SNYK_ORG_ID from env, or pass token=/org_id=
    dataset_name="snyk",
)

answer = await cognee.search(
    query_text="What are the most severe open vulnerabilities?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["snyk"],
)
```

Tokens are region-specific: if your Snyk account lives outside the default
region, pass `base_url=` matching your region (e.g. `https://api.eu.snyk.io/rest`).
See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging with
exactly the issues currently visible to the token on each run. Snyk has no delete
feed, so a fixed or deleted issue simply drops out of the listing and cognee's
existing `orphan_cleanup` removes it from the graph and vector stores. Unchanged
issues keep a stable content-hash `data_id`, so they are not re-ingested or
re-cognified. A request error aborts the run (leaving memory untouched) rather
than letting a partial snapshot forget live issues.

Deliberately, there is no `introduced_since` filter: under `replace`, a partial
result set is indistinguishable from mass remediation. Findings that share a CVE
across projects are deduplicated to their first occurrence before yield.

## Setup

1. Copy your API token from Snyk account settings and your organization ID from
   organization settings. Note the REST API availability depends on your Snyk plan;
   a trial or Enterprise org works.
2. `export SNYK_TOKEN="..." SNYK_ORG_ID="..."`
3. Run `examples/example.py`.
