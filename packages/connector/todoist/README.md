# Todoist Connector for Cognee

Sync your Todoist tasks, projects, and comments into Cognee for persistent, searchable agent memory.

## Features

- **Incremental sync** — Uses Todoist's `/sync` endpoint with `sync_token` cursor for efficient delta updates
- **Forget-on-delete** — Deleted tasks/projects removed from Cognee on next sync
- **Completed tasks retained** — Finished tasks stay in memory (only hard deletions are forgotten)
- **Rich content** — Tasks include description, comments, project context
- **Rate-limit resilient** — Handles 429 with exponential backoff and `Retry-After`

## Installation

```bash
pip install "cognee[todoist]"
# or from source
pip install -e packages/connector/todoist
```

## Quick Start

```python
import asyncio
import os
import cognee
from cognee_community_connector_todoist import todoist_source

DATASET_NAME = "todoist"

async def main() -> None:
    if not os.environ.get("TODOIST_API_KEY"):
        print("Set TODOIST_API_KEY (get it from https://todoist.com/prefs/integrations)")
        return

    source = todoist_source()

    print("Syncing Todoist data into Cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What are my current active tasks?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nComplete or delete a task in Todoist, then re-run: "
        "completed tasks persist, deleted tasks are forgotten."
    )

if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

| Environment Variable | Required | Description |
|---------------------|----------|-------------|
| `TODOIST_API_KEY` | Yes | Your Todoist API token (Bearer token) |
| `LLM_API_KEY` | Yes* | For Cognee's entity extraction (*if not using local models) |

Get your API token: https://todoist.com/prefs/integrations → "API token"

## Sync Behavior

| Event | Cognee Action |
|-------|---------------|
| New task/project | Ingested as document |
| Task edited | Content hash updated → re-ingested |
| Task completed (`checked=true`) | **Retained** in memory |
| Task deleted (`is_deleted=true`) | **Removed** from graph/vector stores |
| Project deleted | **Removed** from memory |
| Comment added/edited | Appended to parent task document |

## Architecture

```
packages/connector/todoist/
├── cognee_community_connector_todoist/
│   ├── __init__.py           # Exports: todoist_source
│   ├── todoist.py            # DLT source factory (main entry)
│   ├── client.py             # Todoist /sync endpoint wrapper
│   ├── render.py             # Task/Project/Comment → Markdown
│   └── reconcile.py          # Deletion detection (_deleted column)
├── examples/
│   └── example.py            # Runnable demo (above)
├── tests/
│   ├── test_todoist_source.py
│   └── test_transform.py
├── pyproject.toml
└── README.md
```

## Development

```bash
# Install dev dependencies
pip install -e "packages/connector/todoist[dev]"

# Run tests
pytest packages/connector/todoist/tests -v

# Lint
ruff check packages/connector/todoist
```

## License

MIT — see LICENSE in root of cognee-community repo.