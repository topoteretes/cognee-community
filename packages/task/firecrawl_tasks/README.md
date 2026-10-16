# cognee-community-tasks-firecrawl

Custom cognee tasks for scraping and searching the web using [Firecrawl](https://www.firecrawl.dev/?utm_source=cognee-community&utm_medium=github&utm_campaign=cognee-firecrawl-tasks), the context API to search, scrape, and interact with the web at scale.

Scrape turns a URL, including JavaScript-rendered pages and PDFs, into clean markdown that cognee can chunk and add to the graph. Search finds sources for a query and returns each page's markdown in the same call, so a question becomes graph-ready context without a second fetch.

## Overview

This package provides four async tasks:

- **`scrape_urls`** scrapes a list of URLs into markdown and returns one structured result per URL.
- **`scrape_and_add`** scrapes a list of URLs and ingests the markdown directly into a cognee dataset.
- **`search_web`** runs a Firecrawl web search and returns structured results, with page markdown by default.
- **`search_and_add`** runs a Firecrawl search and ingests the result pages directly into a cognee dataset.

The same API key also covers batch scrape and full-site crawl, which are not wrapped here yet.

## Installation

```bash
uv pip install cognee-community-tasks-firecrawl
```

Or install locally with all dependencies:

```bash
cd packages/task/firecrawl_tasks
uv sync --all-extras
```

## Requirements

You need two API keys:

| Variable | Description |
|---|---|
| `LLM_API_KEY` | OpenAI (or other LLM provider) API key used by cognee |
| `FIRECRAWL_API_KEY` | [Firecrawl](https://www.firecrawl.dev/app/api-keys?utm_source=cognee-community&utm_medium=github&utm_campaign=cognee-firecrawl-tasks) API key |

Set them in your environment or in a `.env` file:

```bash
export LLM_API_KEY="sk-..."
export FIRECRAWL_API_KEY="fc-..."
```

Every task also accepts an `api_key` argument if you would rather pass the Firecrawl key directly.

## Usage

### Scrape only

```python
import asyncio
from cognee_community_tasks_firecrawl import scrape_urls

pages = asyncio.run(
    scrape_urls(
        urls=["https://docs.cognee.ai/", "https://arxiv.org/pdf/1706.03762"],
        only_main_content=True,
    )
)

for page in pages:
    print(page["url"], page["title"], page["status_code"])
    print(page["content"][:200])
```

### Scrape and add to cognee

```python
import asyncio
from cognee_community_tasks_firecrawl import scrape_and_add

asyncio.run(
    scrape_and_add(
        urls=["https://docs.cognee.ai/"],
        dataset_name="firecrawl",
    )
)
```

### Search and add to cognee

```python
import asyncio
from cognee_community_tasks_firecrawl import search_and_add

asyncio.run(
    search_and_add(
        query="How do knowledge graphs improve LLM memory?",
        limit=5,
        dataset_name="firecrawl_search",
    )
)
```

## Configuration

- `only_main_content` (default `True`) drops navigation, headers and footers so the graph is built from the page body.
- `timeout_ms` (default `30000`) is the per-page scrape timeout in milliseconds.
- `concurrency` (default `5`, at least `1`) caps how many pages `scrape_urls` and `scrape_and_add` fetch at the same time. Keep it at or below the concurrency limit of your Firecrawl plan.
- `limit` (default `5`) is the number of search results.
- `scrape` (default `True`, `search_web` only) returns each result page's markdown with the search. Set it to `False` for titles, URLs and descriptions only.

A URL that fails to scrape does not stop the batch. Its entry has empty `content` and the SDK's error message in `error`. An invalid API key or an exhausted credit balance raises the SDK's error instead, since it would fail every URL. The `*_and_add` tasks skip pages without markdown or with an HTTP error status (404, 500 and so on), cognify only their own dataset, and raise a `RuntimeError` when nothing is left to add.

## Run the example

```bash
cd packages/task/firecrawl_tasks
uv run python examples/example.py
```

## Run the tests

```bash
cd packages/task/firecrawl_tasks
uv run --with pytest pytest tests
```

## API Reference

### `scrape_urls`

```python
async def scrape_urls(
    urls: list[str],
    only_main_content: bool = True,
    timeout_ms: int = 30000,
    concurrency: int = 5,
    api_key: str | None = None,
) -> list[dict]
```

Returns one dict per input URL, in input order:

```python
{
    "url": "https://docs.cognee.ai/",
    "title": "Cognee Documentation",
    "content": "# Cognee ...",  # page markdown, empty on failure
    "description": "...",
    "status_code": 200,
    "error": None,  # failure reason, if any
}
```

### `scrape_and_add`

```python
async def scrape_and_add(
    urls: list[str],
    only_main_content: bool = True,
    timeout_ms: int = 30000,
    concurrency: int = 5,
    api_key: str | None = None,
    dataset_name: str = "firecrawl",
) -> Any
```

Scrapes the URLs, combines every usable page into a single text document, calls `cognee.add`, and then `cognee.cognify` on `dataset_name`. Returns the cognify result.

### `search_web`

```python
async def search_web(
    query: str,
    limit: int = 5,
    scrape: bool = True,
    only_main_content: bool = True,
    api_key: str | None = None,
) -> list[dict]
```

Returns result dicts with the same keys as `scrape_urls`. `content` is empty when `scrape=False`.

### `search_and_add`

```python
async def search_and_add(
    query: str,
    limit: int = 5,
    only_main_content: bool = True,
    api_key: str | None = None,
    dataset_name: str = "firecrawl_search",
) -> Any
```

Runs the search with page markdown enabled, skips results without markdown or with an HTTP error status, combines the rest into a single text document, calls `cognee.add`, and then `cognee.cognify` on `dataset_name`. Returns the cognify result.
