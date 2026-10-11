# cognee-community-connector-logrocket

A Cognee data-source connector for LogRocket's official MCP server. It ingests
structured session metadata and issue reports as normal Cognee documents. Session
replays are not downloaded or stored.

## Requirements

- A LogRocket project-scoped API key from **Settings > API Keys**.
- The LogRocket organization and project IDs.
- A Cognee release with document-mode ingestion (`cognee==1.6.2` is used by this package).
- An `LLM_API_KEY` for Cognee's normal `remember`/`cognify` flow.

The connector uses LogRocket's project-scoped MCP URL,
`https://mcp.logrocket.com/mcp/<org>/<project>?toolsets=sessions,issues`, and
the documented `find_sessions` and `find_issues` tools. The toolset is narrowed
to the selected resources. LogRocket's MCP tool schemas are actively developed;
the connector validates the live schema during initialization instead of assuming
undocumented REST endpoints. If you supply a custom `base_url`, scope it to the
intended project yourself.

## Install

From the `cognee-community` repository root:

```bash
uv pip install -e packages/connector/logrocket
```

For local development:

```bash
cd packages/connector/logrocket
uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_logrocket import logrocket_source

await cognee.remember(
    logrocket_source(
        organization_id="your-organization-id",
        project_id="your-project-id",
        resources=("sessions", "issues"),
    ),
    dataset_name="logrocket",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```

Set `LOGROCKET_API_KEY` instead of passing `api_key`. The source can be scoped to
one resource with `resources=("sessions",)` or `resources=("issues",)`. Provider-
specific MCP filters can be forwarded with `session_filters` and `issue_filters`.
Use `start_time` and `end_time` for session date-window fields advertised by the
live MCP schema. Keep `start_time` and the filters fixed for a dataset; you may
advance `end_time`. A changed scope raises an error before the listing starts.

Keep LogRocket in a dedicated Cognee dataset. `write_disposition="merge"` is
required for incremental updates and hard-delete markers. Each run scans the
selected scope, compares stable row fingerprints in dlt state, and emits only
new or changed documents plus deletions. A complete listing is necessary to
detect upstream deletions; the MCP API does not document a separate deletion
feed. A failed listing or explicit incomplete page aborts without advancing
state. Live LogRocket completeness and graph deletion still need validation
with a real project.

## Example

```bash
export LOGROCKET_API_KEY="your-project-scoped-key"
export LLM_API_KEY="your-llm-key"
export LOGROCKET_ORGANIZATION_ID="your-organization-id"
export LOGROCKET_PROJECT_ID="your-project-id"
uv run python examples/example.py
```

## Testing

The tests use a mocked MCP Streamable HTTP client and do not require a LogRocket
key. Run them from this package directory:

```bash
uv run pytest tests/
uv run ruff check .
uv run ruff format --check .
```
