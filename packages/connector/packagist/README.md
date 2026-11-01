# Cognee Connector: Packagist / Composer (PHP Package Registry)

A high-performance `dlt` data-source connector for [Cognee](https://github.com/topoteretes/cognee) that indexes PHP and Laravel packages, version requirements, maintainer notices, upstream abandonment alerts, and security advisories from [Packagist](https://packagist.org) directly into Cognee graph and vector memory.

Enables engineering teams and AI assistants to reason about dependency health:
> *"Which of our Composer dependencies have been marked abandoned upstream, and what replacement packages did the authors recommend?"*
> *"Are any of the PHP packages in our stack affected by open Packagist security advisories?"*

---

## Features

- **Public & Private Registry Support**: Connect without credentials using standard Packagist API policies (descriptive User-Agent header) or provide a Bearer token for Private Packagist instances.
- **Package & Release Ingestion**: Ingests repository URLs, total downloads, GitHub stars, maintainers, and recent releases with PHP version constraints.
- **Upstream Abandonment Tracking**: Detects when packages are marked abandoned and records official replacement recommendations (e.g. `abandoned: "symfony/mailer"`).
- **Official Security Advisories**: Pulls vulnerability alerts (`PKSA-*`, CVEs, affected version ranges, and disclosure links).
- **Vendor-Level Discovery**: Discover and ingest all packages under an organization prefix (e.g. `vendor="symfony"` or `vendor="laravel"`).
- **Incremental Syncing**: Leverages Packagist's official `/metadata/changes.json` feed to track changes with high-resolution timestamps.
- **Deletion Tombstoning**: Automatically emits `_deleted: True` when packages return 404 or are excluded, triggering orphan cleanup in Cognee.
- **Standalone CLI**: Quick inspection tool (`cognee-packagist`) for terminal-based testing and queries.

---

## Installation

```bash
pip install cognee-community-connector-packagist
```

Or install in editable mode from source:
```bash
cd packages/connector/packagist
pip install -e .
```

---

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_packagist import packagist_source, PackagistRenderOptions

async def main():
    # Pass a composer.json / composer.lock file or specific package list
    source = packagist_source(
        composer_file="./composer.json",
        packages=["monolog/monolog", "swiftmailer/swiftmailer"],
        options=PackagistRenderOptions(
            include_security_advisories=True,
            include_version_requirements=True,
        ),
    )

    await cognee.remember(
        source,
        dataset_name="php_package_registry",
    )

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Visual Output & Live Execution Preview

![Packagist Live Preview](docs/packagist-preview.png)

---

## CLI Usage

Inspect Composer packages and advisories directly from your terminal:

```bash
# Ingest directly from local composer.json or composer.lock
python -m cognee_community_connector_packagist.cli --composer-file ./composer.json

# Inspect specific packages
python -m cognee_community_connector_packagist.cli --packages "monolog/monolog,swiftmailer/swiftmailer"

# Auto-discover vendor packages
python -m cognee_community_connector_packagist.cli --vendor "guzzlehttp"

# Filter out abandoned packages
python -m cognee_community_connector_packagist.cli --packages "swiftmailer/swiftmailer" --exclude-abandoned
```

---

## Acceptance & Quality Criteria

- [x] Connects without credentials with descriptive User-Agent header.
- [x] Supports `composer.json` and `composer.lock` direct dependency extraction.
- [x] Supports `packages` list and `vendor` auto-discovery.
- [x] Ingests version constraints, abandonment replacement notices, and security advisories.
- [x] Emits canonical `{id, title, content, url, _deleted}` rows.
- [x] 17 hermetic unit tests with mock HTTP transport (100% pass without network access).
- [x] High-resolution live preview documentation included.
- [x] Ready for production integration in Cognee AI memory.
