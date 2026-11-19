# cognee-community-connector-todoist

A **Todoist** task data-source connector for **cognee**: sync your tasks, projects,
priorities, and deadlines into memory — *"what's due this week across the Robotics project?"*

It exposes a **dlt source** you hand to `cognee.remember(...)` / `cognee.add(...)`.
Todoist tasks are ingested as **normal documents** via cognee's document-mode marker.

## Requirements

Requires cognee ≥ 1.4.0 (document-mode support).
A Todoist personal API token (from Settings → Integrations → Developer).

## Installation

```bash
pip install "cognee[todoist] @ git+https://github.com/topoteretes/cognee-community.git#subdirectory=packages/connector/todoist"
```

## Authentication

```bash
export TODOIST_API_TOKEN="your-personal-api-token"
```

## Usage

```python
import cognee
from cognee_community_connector_todoist import todoist_source

# Configure cognee first

# Ingest all active tasks
source = todoist_source()
await cognee.add(source)
```

Or scope to specific projects:

```python
source = todoist_source(project_ids=["220477321", "220477322"])
```

## Sync model: merge + _deleted tombstones

This connector uses ``write_disposition="merge"`` with a ``_deleted`` boolean column
(rather than a full-snapshot ``replace``) so incremental syncs are cheap and safe.

Each run performs three phases:

1. **Re-check in-progress items**: Tasks that had no due date or a future due date on
   the previous run are fetched individually. Their state may have advanced (completed,
   new comments, description edited).
2. **Incremental fetch**: ``GET /tasks?since=<cursor>`` pulls anything updated since the
   last sync (minus a 5-minute overlap window to catch late-updating items).
3. **Deletion detection** (first-run only): On the initial full sync, tasks that were
   previously known but no longer appear in the active listing receive a
   ``_deleted=True`` tombstone. cognee's ingestion pipeline removes these from the graph.

### Safety guarantee

> A transient API error **aborts the run before any tombstone is emitted**.
> A partial snapshot must never drive mass deletions. Permanent errors (401/403 on
> the root endpoint) raise immediately. A 404 on an individual in-progress task is
> treated as "gone" and tombstoned safely.

### Edge cases documented

- **Recurring tasks**: Each occurrence carries its own due date; the connector treats
  each as a separate document update.
- **Completed tasks**: Todoist's ``/tasks`` endpoint does not return completed items by
  default. A completed task disappears from the active listing and will be tombstoned
  on the *next full sync*. On incremental syncs, a task that was re-checked and found
  completed is emitted with ``is_completed=True`` but NOT deleted — the text is still
  valuable memory.
- **Comments**: ``comment_count`` is included in metadata but comment bodies are NOT
  fetched (Todoist API requires a separate call per task). The count signals activity.
- **Multi-project scoping**: When ``project_ids`` contains multiple projects, the
  connector fetches each project's tasks sequentially. Deletion detection is
  per-project on first sync.

## Data model (document-mode)

One document per Todoist task containing:
- ``id``, ``project_id``, ``section_id``
- ``content`` (title), ``description`` (body)
- ``priority`` (1-4), ``priority_label`` (low/normal/high/urgent)
- ``due_date``, ``due_recurring``
- ``labels`` list, ``assignee_ids`` list
- ``created_at``, ``url``, ``comment_count``
- ``is_completed``, ``_deleted`` tombstone flag
- Full raw task preserved in ``raw`` field

## Layout

```
packages/connector/todoist/
├── README.md
├── cognee_community_connector_todoist/
│   ├── __init__.py
│   └── todoist.py
├── examples/
│   └── example.py
├── tests/
│   ├── fixtures/           # Recorded API responses for offline tests
│   └── test_todoist.py
└── pyproject.toml
```

## Reference implementation

Read ``packages/connector/notion/`` first for the document-mode pattern. This
connector extends that pattern with ``merge`` + ``_deleted`` tombstones and
in-progress item re-checking, learned from the CircleCI connector implementation.
