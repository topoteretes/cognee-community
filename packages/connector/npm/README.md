# npm Package Registry Community Connector for Cognee

A lightweight, zero-dependency REST connector that connects the **npm Registry** with **Cognee**. Ingest package manifests, latest version metadata, dependency mappings, licenses, and full documentation READMEs into Cognee's cognitive memory graph.

## Features

- **Public & Zero-Auth**: Queries the public npm registry (`https://registry.npmjs.org/`) with no API keys or developer accounts required.
- **Scoped Package Support**: Safely parses and URL-encodes scoped packages (e.g. `@angular/core`, `@babel/preset-env`).
- **Complete Dependency Graphs**: Ingests production `dependencies`, `devDependencies`, and `peerDependencies`.
- **Rich Documentation Ingestion**: Extracts full package README markdown files to empower AI code copilots with setup guides and API specifications.
- **Resilient Sync**: Missing or unpublished packages (HTTP 404) are skipped gracefully without aborting snapshot ingestion.
- **Hermetic Pytest Suite**: 100% offline mock test suite covering single packages, scoped packages, 404 skips, rate limits, and Markdown rendering.

## Installation

```bash
pip install cognee-community-connector-npm
```

## Quickstart

```python
import asyncio
from cognee_community_connector_npm import NpmConnector

async def main():
    tracked_packages = ["react", "express", "zod", "@angular/core"]

    connector = NpmConnector(packages=tracked_packages, include_readme=True)

    # Ingest package manifests and READMEs into Cognee
    documents = await connector.to_documents()

    # await cognee.add(documents)
    # await cognee.cognify()

    await connector.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Execute the hermetic unit tests offline:

```bash
pytest packages/connector/npm/tests/test_npm.py -v
```
