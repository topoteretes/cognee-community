# cognee-community-connector-artifactory

JFrog Artifactory data-source connector for [cognee](https://github.com/topoteretes/cognee).

Syncs binary repository topologies, package artifact metadata with cryptographic checksums, and CI/CD build provenance (SBOM dependencies) into Cognee memory and knowledge graphs.

## Features

- **Repository Topologies**: Ingests local, remote, and virtual repositories across Docker, Maven, npm, PyPI, Helm, and generic ecosystems.
- **Cryptographic Package Integrity**: Extracts package coordinates, file sizes, timestamps, and SHA-256 / SHA-1 / MD5 checksums.
- **CI/CD Build Provenance & SBOM**: Ingests build run execution metadata, VCS commit URLs, published modules, and declared dependencies for software supply chain observability.
- **Forget-On-Delete**: Uses `write_disposition="replace"` and `cognee_document_source = "artifactory"` so deleted or decommissioned packages automatically drop out of Cognee's graph via automatic orphan cleanup.
- **Selective Scoping**: Filter ingestion by repository keys, repository type (`local`, `remote`, `virtual`), package ecosystem, or specific build names.
- **Security & Integrity**: Automatic credential redaction (tokens, API keys, passwords) and zero external code execution.

## Installation

```bash
pip install cognee-community-connector-artifactory
```

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_artifactory import artifactory_source


async def main():
    # Ingest repositories, artifacts, and build runs
    source = artifactory_source(
        base_url="https://company.jfrog.io/artifactory",
        token="your-jfrog-access-token",
        repo_keys=["docker-prod-local", "libs-release-local"],
        include_artifacts=True,
        include_builds=True,
    )

    await cognee.add(source)
    await cognee.cognify()

    # Query your software supply chain
    results = await cognee.search(
        "Which Docker image was published from commit 9f8a2b and what are its dependencies?"
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

## Environment Variables

- `ARTIFACTORY_URL`: JFrog Artifactory server base URL (e.g. `https://company.jfrog.io/artifactory`).
- `ARTIFACTORY_TOKEN`: JFrog Access Token.
- `ARTIFACTORY_API_KEY`: Optional legacy API key (`X-JFrog-Art-Api`).
- `ARTIFACTORY_USERNAME` / `ARTIFACTORY_PASSWORD`: Optional basic authentication credentials.
