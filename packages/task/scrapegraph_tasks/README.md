# cognee-community-tasks-scrapegraph

Custom cognee tasks for scraping web content using [ScrapeGraphAI](https://github.com/ScrapeGraphAI/scrapegraph-py).

## Overview

This package provides two async tasks:

- **`scrape_urls`** – scrape a list of URLs with a natural language prompt and return structured results.
- **`scrape_and_add`** – scrape URLs and ingest the content directly into a cognee dataset.

## Installation

```bash
uv pip install cognee-community-tasks-scrapegraph
```

Or install locally with all dependencies:

```bash
cd packages/task/scrapegraph_tasks
uv sync --all-extras
# OR
poetry install
```

## Requirements

You need two API keys:

| Variable | Description |
|---|---|
| `LLM_API_KEY` | OpenAI (or other LLM provider) API key used by cognee |
| `SGAI_API_KEY` | [ScrapeGraphAI](https://scrapegraphai.com) API key |

Set them in your environment or in a `.env` file:

```bash
export LLM_API_KEY="sk-..."
export SGAI_API_KEY="sgai-..."
```

## Usage

### Scrape only

```python
import asyncio
from cognee_community_tasks_scrapegraph import scrape_urls

results = asyncio.run(
    scrape_urls(
        urls=["https://cognee.ai", "https://docs.cognee.ai"],
        user_prompt="Extract the main content, title, and key information from this page",
    )
)

for item in results:
    print(item["url"], item["content"])
```

### Scrape and add to cognee

```python
import asyncio
from cognee_community_tasks_scrapegraph import scrape_and_add

asyncio.run(
    scrape_and_add(
        urls=["https://cognee.ai"],
        user_prompt="Extract the main content and key information",
        dataset_name="web_scrape",
    )
)
```

## Run the example

```bash
cd packages/task/scrapegraph_tasks
uv run python examples/example.py
# OR
poetry run python examples/example.py
```

## API Reference

### `scrape_urls`

```python
async def scrape_urls(
    urls: List[str],
    user_prompt: str = "Extract the main content, title, and key information from this page",
    api_key: Optional[str] = None,
) -> List[dict]
```

Returns a list of dicts:

```python
[
    {"url": "https://example.com", "content": {...}},  # success
    {"url": "https://bad.invalid", "content": "", "error": "..."},  # failure
]
```

### `scrape_and_add`

```python
async def scrape_and_add(
    urls: List[str],
    user_prompt: str = "Extract the main content, title, and key information from this page",
    api_key: Optional[str] = None,
    dataset_name: str = "scrapegraph",
) -> Any
```

Scrapes all URLs, combines the successful results into a single text document, calls `cognee.add`, and then `cognee.cognify`. Returns the cognify result.
