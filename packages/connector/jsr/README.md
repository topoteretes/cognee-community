# Cognee Community Connector: JSR (JavaScript Registry)

A native data-source connector for [Cognee](https://github.com/topoteretes/cognee) to ingest TypeScript and JavaScript package specifications, documentation, runtime compatibility matrices, and exports from the [JSR (JavaScript Registry)](https://jsr.io) into knowledge graphs.

## Features
- **Scope & Package Scoping:** Ingest entire organization scopes (e.g. `@std`) or individual targeted packages (e.g. `@std/path`, `@std/fs`, `@std/assert`).
- **Rich Documentation Normalization:** Normalizes package summaries, descriptions, runtime compatibility (Deno, Node.js, Bun, Browser), repository links, exports, and README docs into unified document structures.
- **Incremental Synchronization:** Content-hashed SHA-256 fingerprinting skips unchanged package versions on subsequent syncs.
- **Forget-on-Delete:** Deletion reconciliation emits `_deleted: True` tombstones when packages are removed upstream or deselected from configuration.
- **100% Hermetic & Offline:** Testable completely offline with zero live credentials required.

## Installation

```bash
pip install cognee-community-connector-jsr
```

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_jsr import jsr_source


async def main():
    # Configure JSR source
    source = jsr_source(
        scopes=["std"],
        packages=["@std/path", "@std/fs"],
        include_docs=True,
    )

    # Ingest and cognify into memory
    await cognee.add(source)
    await cognee.cognify()

    # Query in natural language
    results = await cognee.search("What path utilities are available in @std/path?")
    for r in results:
        print(r)


if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `scopes` | `list[str]` | `None` | JSR scopes to ingest (e.g. `["std", "oak"]`). |
| `packages` | `list[str]` | `None` | Explicit package names (e.g. `["@std/path"]`). |
| `include_docs` | `bool` | `True` | Whether to fetch detailed exports and README documentation. |
| `base_url` | `str` | `"https://api.jsr.io"` | JSR API base endpoint. |
| `client` | `JSRClient` | `None` | Optional injected HTTP client instance for testing. |

## License
Apache-2.0
