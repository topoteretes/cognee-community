# cognee-community-tasks-exa

Custom cognee tasks for searching the web using [Exa](https://exa.ai).

## Overview

This package provides two async tasks:

- **`search_web`** – run a neural/keyword/hybrid web search with Exa and return structured results.
- **`search_and_add`** – run an Exa search and ingest the returned content directly into a cognee dataset.

## Installation

```bash
uv pip install cognee-community-tasks-exa
```

Or install locally with all dependencies:

```bash
cd packages/task/exa_tasks
uv sync --all-extras
# OR
poetry install
```

## Requirements

You need two API keys:

| Variable | Description |
|---|---|
| `LLM_API_KEY` | OpenAI (or other LLM provider) API key used by cognee |
| `EXA_API_KEY` | [Exa](https://dashboard.exa.ai) API key |

Set them in your environment or in a `.env` file:

```bash
export LLM_API_KEY="sk-..."
export EXA_API_KEY="..."
```

## Usage

### Search only

```python
import asyncio
from cognee_community_tasks_exa import search_web

results = asyncio.run(
    search_web(
        query="Latest advances in retrieval augmented generation",
        num_results=5,
        include_highlights=True,
        category="research paper",
    )
)

for item in results:
    print(item["url"], item["title"])
    print(item["content"][:200])
```

### Search and add to cognee

```python
import asyncio
from cognee_community_tasks_exa import search_and_add

asyncio.run(
    search_and_add(
        query="How do knowledge graphs improve LLM memory?",
        num_results=5,
        dataset_name="exa_search",
    )
)
```

## Run the example

```bash
cd packages/task/exa_tasks
uv run python examples/example.py
# OR
poetry run python examples/example.py
```

## API Reference

### `search_web`

```python
async def search_web(
    query: str,
    num_results: int = 10,
    search_type: str = "auto",
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    category: Optional[str] = None,
    start_published_date: Optional[str] = None,
    end_published_date: Optional[str] = None,
    include_text: bool = True,
    include_highlights: bool = False,
    include_summary: bool = False,
    summary_query: Optional[str] = None,
    api_key: Optional[str] = None,
) -> List[dict]
```

Returns a list of dicts with keys:

```python
{
    "url": "https://example.com/post",
    "title": "Example post",
    "content": "Best available text/summary/highlights...",
    "text": "...",  # full text, if requested
    "highlights": [...],  # list, if requested
    "summary": "...",  # LLM summary, if requested
    "score": 0.92,
    "published_date": "2026-01-12",
    "author": "Alice",
}
```

Search types: `auto` (default), `neural`, `fast`, `deep-lite`, `deep`, `deep-reasoning`, `instant`.

Categories: `company`, `research paper`, `news`, `personal site`, `financial report`, `pdf`, `github`, `tweet`, `movie`, `song`, `linkedin profile`.

### `search_and_add`

```python
async def search_and_add(
    query: str,
    num_results: int = 10,
    search_type: str = "auto",
    include_domains: Optional[List[str]] = None,
    exclude_domains: Optional[List[str]] = None,
    category: Optional[str] = None,
    start_published_date: Optional[str] = None,
    end_published_date: Optional[str] = None,
    summary_query: Optional[str] = None,
    api_key: Optional[str] = None,
    dataset_name: str = "exa",
) -> Any
```

Runs the search with text + highlights enabled, combines every result with content into a single text document, calls `cognee.add`, and then `cognee.cognify`. Returns the cognify result.
