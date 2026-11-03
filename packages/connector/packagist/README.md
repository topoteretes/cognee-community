# Packagist / Composer Connector for Cognee

An enterprise connector for ingesting PHP and Laravel dependency metadata from **Packagist** (`packagist.org`) into Cognee's graph memory.

## Features

- **Public & Private Registry Support**: Connect without credentials for public packages; supports optional bearer tokens for Private Packagist.
- **Dependency & Release Graphing**: Maps `require`, `suggest`, `conflict`, and PHP version constraints across package releases.
- **Abandonment Detection**: Ingests maintainer abandonment flags and recommended replacement packages (e.g. `swiftmailer/swiftmailer` -> `symfony/mailer`).
- **Security Advisories**: Integrates official Packagist Security Advisories (`PKSA-*` / CVEs) directly into package memory.
- **Incremental Sync & Deletion Tombstones**: Tracks upstream `/metadata/changes.json` and emits `_deleted: True` tombstones for removed or excluded packages.

## Quick Start

```python
import asyncio
from cognee_community_connector_packagist import packagist_source

async def main():
    # Ingest popular composer packages with security advisories
    source = packagist_source(packages=["monolog/monolog", "symfony/mailer"])
    for doc in source:
        print(f"Ingested {doc['name']} - Abandoned: {doc['abandoned']} - Advisories: {doc['advisory_count']}")

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

```bash
python3 -m pytest packages/connector/packagist/tests/ -v
```

## License

Apache-2.0
