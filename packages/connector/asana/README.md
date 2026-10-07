# cognee-community-connector-asana

Sync selected Asana projects into [cognee](https://github.com/topoteretes/cognee) so
their project descriptions, tasks, subtasks, and comment threads are searchable as
normal documents.

The connector uses Asana's read-only REST API with a personal access token. Re-running
the same source performs an incremental sync: task changes use Asana's `modified_since`
filter, comment fingerprints catch comment-only changes, and a lightweight ID inventory
detects tasks or subtasks that disappeared. Deleted objects are removed from cognee on
the next successful sync.

## Install

From PyPI after the package is published:

```bash
uv pip install cognee-community-connector-asana
```

From this repository:

```bash
cd packages/connector/asana
uv sync
```

## Asana setup

1. Create a personal access token in the Asana developer console.
2. Copy the GIDs of the projects you want to ingest, or the GID of one workspace.
3. Export the token and your normal cognee LLM credentials:

```bash
export ASANA_ACCESS_TOKEN="your-asana-personal-access-token"
export LLM_API_KEY="your-llm-api-key"
```

Treat the Asana token as a password. The connector never includes it in rows or logs and
only performs `GET` requests.

## Usage

Select explicit projects:

```python
import cognee
from cognee_community_connector_asana import asana_source

source = asana_source(project_ids=["1200123456789012"])

await cognee.remember(
    source,
    dataset_name="asana",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```

Or ingest every project accessible in one workspace:

```python
source = asana_source(workspace_id="1200987654321098")
```

Pass exactly one of `project_ids` or `workspace_id`. Explicit project selection is safer
for production because it prevents a newly created workspace project from being ingested
without review.

See [`examples/example.py`](examples/example.py) for ingestion and search together.

## Synchronization model

The source stores its cursor and inventory in dlt resource state:

- Project and task GIDs become stable keys (`project:<gid>` and `task:<gid>`).
- Tasks and recursively discovered subtasks are separate documents. Their comments are
  included in the corresponding task document.
- `modified_since` finds normal task changes. The connector also compares the lightweight
  `modified_at` value returned by the inventory sweep.
- Comment stories are polled independently and fingerprinted because comment-only changes
  are not safe to infer from the task cursor alone.
- Missing projects/tasks produce dlt hard-delete tombstones. With merge mode, they leave
  the staging table, after which cognee's normal orphan cleanup removes their graph,
  vector, and relational data.
- State advances only after every API read succeeds and the source is fully consumed. A
  partial or failed listing cannot be mistaken for deletion.

Always pass `write_disposition="merge"`, `primary_key="id"`, and
`max_rows_per_table=0` to `cognee.remember`. The unlimited read is important: orphan
cleanup must compare against the complete staged corpus rather than a truncated page.

## Provider behavior and cost

The correctness-oriented comment sweep makes one stories-list request per current task or
subtask on each sync. This prevents missed comment-only edits but can be request-heavy for
very large projects. Asana pagination, `Retry-After`, rate limits, and transient server
errors are handled; narrow `project_ids` are recommended for large workspaces.

## Testing

Tests use a fake Asana API and a temporary SQLite dlt destination; no live token is needed:

```bash
uv run pytest tests/ -v
uv run ruff check .
uv run ruff format --check .
```

The suite covers pagination, workspace/project selection, initial ingestion, unchanged
re-sync, the incremental cursor, comment-only updates, recursive subtasks, retries, and
forget-on-delete.
