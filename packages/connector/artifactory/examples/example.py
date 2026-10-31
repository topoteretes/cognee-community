"""Example usage of the Cognee JFrog Artifactory connector."""

import asyncio
import os

import cognee

from cognee_community_connector_artifactory import artifactory_source


async def main():
    artifactory_url = os.environ.get("ARTIFACTORY_URL", "https://company.jfrog.io/artifactory")
    token = os.environ.get("ARTIFACTORY_TOKEN", "mock-token")

    # Ingest repository configurations, artifacts with cryptographic checksums, and CI/CD builds
    source = artifactory_source(
        base_url=artifactory_url,
        token=token,
        repo_keys=["docker-prod-local", "maven-release-local"],
        include_artifacts=True,
        include_builds=True,
    )

    print("Adding JFrog Artifactory packages and builds to Cognee...")
    await cognee.add(source)

    print("Running Cognify on software supply chain graph...")
    await cognee.cognify()

    print("Querying package provenance and SBOM dependencies...")
    results = await cognee.search(
        "Which Docker image tag in docker-prod-local was built from git commit 9f8a2b?"
    )
    for res in results:
        print("-", res)


if __name__ == "__main__":
    asyncio.run(main())
