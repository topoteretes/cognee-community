# cognee-community-tasks-keenable

Custom cognee tasks for searching the web using [Keenable](https://keenable.ai).

This is a standalone community package. Installing or importing it does not
change cognee's default search behaviour.

## Overview

This package provides two async tasks:

- **`search_web`** – run a web search with Keenable and return structured results.
- **`search_and_add`** – run a Keenable search and ingest the returned content directly into a cognee dataset.

An API key is optional. Without `KEENABLE_API_KEY`, requests go to the public
search endpoint and send the `X-Keenable-Title` header that endpoint requires.
With a key, they use the keyed `/v1/search` endpoint instead.

## Installation

```bash
uv pip install cognee-community-tasks-keenable
```

Or install locally with all dependencies:

```bash
cd packages/task/keenable_tasks
uv sync --all-extras
# OR
poetry install
```

## Requirements

| Variable | Description |
|---|---|
| `LLM_API_KEY` | OpenAI (or other LLM provider) API key used by cognee, needed for `search_and_add` |
| `KEENABLE_API_KEY` | Optional [Keenable](https://keenable.ai) API key |

```bash
export LLM_API_KEY="sk-..."
# export KEENABLE_API_KEY="..."   # optional
```

## Usage

### Search only

```python
import asyncio
from cognee_community_tasks_keenable import search_web

results = asyncio.run(
    search_web(
        query="Latest advances in retrieval augmented generation",
        num_results=5,
    )
)

for item in results:
    print(item["url"], item["title"])
    print(item["content"][:200])
```

### Search and add to cognee

```python
import asyncio
from cognee_community_tasks_keenable import search_and_add

asyncio.run(
    search_and_add(
        query="How do knowledge graphs improve LLM memory?",
        num_results=5,
        dataset_name="keenable_search",
    )
)
```

## Run the example

```bash
cd packages/task/keenable_tasks
uv run python examples/example.py
```

## Tests

```bash
cd packages/task/keenable_tasks
uv run pytest tests/test_keenable.py
```
