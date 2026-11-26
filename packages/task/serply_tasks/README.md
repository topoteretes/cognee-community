# cognee-community-tasks-serply

Custom cognee tasks for searching Google using [Serply](https://serply.io).

## Overview

This package provides two async tasks:

- **`search_web`** - run a Google web or news search with Serply and return structured results, optionally with each result page as markdown.
- **`search_and_add`** - run a Serply search and ingest the result pages directly into a cognee dataset.

## Installation

```bash
uv pip install cognee-community-tasks-serply
```

Or install locally with all dependencies:

```bash
cd packages/task/serply_tasks
uv sync --all-extras
# OR
poetry install
```

## Requirements

You need two API keys:

| Variable | Description |
|---|---|
| `LLM_API_KEY` | OpenAI (or other LLM provider) API key used by cognee |
| `SERPLY_API_KEY` | [Serply](https://serply.io) API key |

Set them in your environment or in a `.env` file:

```bash
export LLM_API_KEY="sk-..."
export SERPLY_API_KEY="..."
```

## Usage

### Search only

```python
import asyncio
from cognee_community_tasks_serply import search_web

results = asyncio.run(
    search_web(
        query="Latest advances in retrieval augmented generation",
        num_results=5,
        time_range="month",
    )
)

for item in results:
    print(item["url"], item["title"])
    print(item["description"])
```

Google operators work inside the query, for example `site:arxiv.org graph memory`.

### Search and add to cognee

```python
import asyncio
from cognee_community_tasks_serply import search_and_add

asyncio.run(
    search_and_add(
        query="How do knowledge graphs improve LLM memory?",
        num_results=5,
        dataset_name="serply_search",
    )
)
```

## Run the example

```bash
cd packages/task/serply_tasks
uv run python examples/example.py
# OR
poetry run python examples/example.py
```

## API Reference

See the [Serply API docs](https://serply.io/docs) for the underlying endpoints.

### `search_web`

```python
async def search_web(
    query: str,
    num_results: int = 10,
    search_type: str = "web",
    country: Optional[str] = None,
    time_range: Optional[str] = None,
    include_content: bool = False,
    concurrency: int = 5,
    timeout: float = 60,
    api_key: Optional[str] = None,
) -> List[dict]
```

Returns a list of dicts with keys:

```python
{
    "url": "https://example.com/post",
    "title": "Example post",
    "content": "Page markdown if fetched, otherwise the search snippet",
    "description": "Search snippet",
    "position": 1,
    "published": "2 days ago",  # news results only
    "error": None,  # set when the page fetch failed
}
```

Search types: `web` (default), `news`.

Time ranges: `day`, `week`, `month`, `year`.

`num_results` is capped at 10, the most Serply returns per request. With `include_content=True` each result page is fetched as markdown through Serply, one extra request per page. A page that fails to load keeps its snippet as `content` and records the error. An invalid API key or exhausted credits raises instead.

### `search_and_add`

```python
async def search_and_add(
    query: str,
    num_results: int = 10,
    search_type: str = "web",
    country: Optional[str] = None,
    time_range: Optional[str] = None,
    concurrency: int = 5,
    timeout: float = 60,
    api_key: Optional[str] = None,
    dataset_name: str = "serply",
) -> Any
```

Runs the search with page content enabled, combines every result with content into a single text document, calls `cognee.add`, and then `cognee.cognify`. Returns the cognify result.
