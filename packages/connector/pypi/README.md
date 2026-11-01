# cognee-community-connector-pypi

A PyPI exact-release metadata connector for [cognee](https://github.com/topoteretes/cognee):
ingest package releases, descriptions, declared dependencies, and upstream metadata into Cognee memory.

## Features

- **No API key required**: Reads the public, unauthenticated PyPI JSON API (`https://pypi.org/pypi/{project}/{version}/json`).
- **PEP 503 normalization**: Canonicalizes project names (e.g. `requests_toolbelt`, `Requests-Toolbelt`, `requests.toolbelt`) according to PyPA specifications.
- **Identity validation**: Confirms that returned metadata matches the queried project name and exact version before ingesting.
- **Rich document rendering**: Emits structured Markdown documents with:
  - Project summary & full description
  - Python version constraints (`Requires-Python`)
  - Declared dependencies (`requires_dist`)
  - Classifiers and license information
  - Project URLs (Repository, Documentation, Homepage)
  - Distribution filenames, sizes, upload timestamps, and SHA256 digests
  - Explicit warnings for **yanked releases** (with publisher yank reasons)
- **Document mode integration**: Opts into Cognee's document mode (`DOCUMENT_SOURCE_ATTR = "pypi"`), routing records through chunking and LLM knowledge graph extraction.
- **Deterministic snapshot sync**: Uses `write_disposition="replace"`, allowing orphan cleanup to purge removed releases when configured inventories change.

## Installation

```bash
uv pip install cognee-community-connector-pypi

# Or for local development from the repository root:
cd packages/connector/pypi
uv sync --all-extras
```

## Quick Start

```python
import asyncio
import cognee
from cognee_community_connector_pypi import pypi_source

# Specify release pairs or spec strings
RELEASES = [
    ("cognee", "1.6.3"),
    "requests==2.31.0",
]


async def main():
    # Ingest PyPI releases into a dedicated dataset
    await cognee.remember(
        pypi_source(RELEASES),
        dataset_name="pypi_metadata",
        max_rows_per_table=0,
    )

    # Ask questions against the ingested package knowledge graph
    answer = await cognee.recall(
        "What Python versions are supported by cognee 1.6.3 and what are its dependencies?",
        datasets=["pypi_metadata"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
```

See `examples/example.py` for a complete runnable demo.

## How Sync and Deletion Work

The connector operates as a deterministic snapshot source (`write_disposition="replace"`):
1. All configured releases are fetched and validated against PyPI before yielding records.
2. If a network failure, rate exhaustion, or 404 occurs during fetching, existing memory is preserved.
3. Removing a package from your configured list drops it on the next sync, triggering Cognee's orphan cleanup to remove its nodes and relations from memory.

## Running Tests

```bash
uv run pytest packages/connector/pypi/tests/ -v
```
