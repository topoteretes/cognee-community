# cognee-community-connector-logrocket

A Cognee data-source connector for LogRocket's official MCP server. It ingests
structured session metadata and issue reports as normal Cognee documents. Session
replays are not downloaded or stored.

## Requirements

- A LogRocket project-scoped API key from **Settings > API Keys**.
- The LogRocket organization and project IDs.
- A Cognee release with document-mode ingestion (`cognee==1.6.2` is used by this package).
- An `LLM_API_KEY` for Cognee's normal `remember`/`cognify` flow.

The connector uses `https://mcp.logrocket.com/mcp` and the documented `find_sessions`
and `find_issues` MCP tools. LogRocket's MCP tool schemas are actively developed;
the connector validates the live schema during initialization instead of assuming
undocumented REST endpoints.

## Install

```bash
uv pip install cognee-community-connector-logrocket
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
    max_rows_per_table=0,
)
```

Set `LOGROCKET_API_KEY` instead of passing `api_key`. The source can be scoped to
one resource with `resources=("sessions",)` or `resources=("issues",)`. Provider-
specific MCP filters can be forwarded with `session_filters` and `issue_filters`.
Use `start_time` and `end_time` for the supported date-window fields advertised by
the live MCP schema.

Keep LogRocket in a dedicated Cognee dataset. The selected date/filter scope is an
authoritative snapshot: records that disappear from that scope on a successful sync
are eligible for Cognee's existing orphan cleanup. A failed or partial MCP response
aborts the run and does not advance any source state or trigger cleanup.

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
