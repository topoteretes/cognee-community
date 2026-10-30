# Cognee Community Vector Adapter - Moss

Use [Moss](https://usemoss.dev) as the vector database for [Cognee](https://github.com/topoteretes/cognee).

Moss is a vector search service - indexes are built in the cloud and loaded locally for sub-10ms queries. No infrastructure to manage. Create an account, grab your credentials, and plug them in.

## Prerequisites

1. Create a Moss account at [portal.usemoss.dev](https://portal.usemoss.dev)
2. Create a project and copy your **Project ID** and **Project Key**
3. An OpenAI API key (Cognee uses it for LLM and embeddings by default)

## Installation

```bash
pip install cognee-community-vector-adapter-moss
```

Or install from source:

```bash
cd packages/vector/moss
pip install -e .
```

## Configuration

Set environment variables:

```bash
export MOSS_PROJECT_ID="your-project-id"
export MOSS_PROJECT_KEY="your-project-key"
export LLM_API_KEY="your-openai-api-key"
```

Or create a `.env` file (see `.env.example`).

## Usage

```python
import asyncio
import os
from cognee_community_vector_adapter_moss import register  # noqa: F401
from cognee import add, cognify, config, search


async def main():
    config.set_vector_db_config(
        {
            "vector_db_provider": "moss",
            "vector_db_key": os.getenv("MOSS_PROJECT_KEY"),
            "vector_db_name": os.getenv("MOSS_PROJECT_ID"),
            "vector_dataset_database_handler": "moss",
        }
    )

    await add("Natural language processing is a subfield of computer science.")
    await cognify()

    results = await search(query_text="Tell me about NLP")
    for r in results:
        print(r)


asyncio.run(main())
```

## How It Works

The adapter multiplexes all Cognee collections into a **single Moss index** (`cognee-index-{timestamp}`). This keeps usage within Moss's free tier (3 indexes). Each document is tagged with a `_collection` metadata field, and all searches filter by it.

- Embeddings are computed by Cognee's embedding engine and passed to Moss via `DocumentInfo(embedding=[...])`
- The index is loaded locally via `load_index(auto_refresh=True)` for fast sub-10ms queries
- Async Moss jobs (`create_index`, `add_docs`) are polled to completion internally

## Running Tests

```bash
MOSS_PROJECT_ID="..." MOSS_PROJECT_KEY="..." LLM_API_KEY="..." python tests/test_moss.py
```

Tests cover: vector search (text + vector), nodeset filtering, graph completion, chunks, summaries, and prune cleanup.

## Resources

- [Moss Documentation](https://docs.moss.dev)
- [Moss Quickstart](https://docs.moss.dev/docs/start/quickstart)
- [Cognee Documentation](https://docs.cognee.dev)
