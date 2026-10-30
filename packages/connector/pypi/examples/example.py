"""Ingest exact PyPI package release metadata into cognee memory via the dlt connector.

PyPI provides a public, unauthenticated JSON API (https://docs.pypi.org/api/json/)
that exposes publisher release descriptions, Python version constraints (Requires-Python),
declared dependencies (requires_dist), distribution files, and yanked status.

This connector enables AI research agents to accurately answer questions about
specific package releases, dependency boundaries, and upstream project metadata
without having to install packages or execute untrusted code.

Usage:
    uv run python examples/example.py

Prerequisites:
    - LLM_API_KEY set in your environment (for cognee graph extraction and recall).
"""

import asyncio

import cognee

from cognee_community_connector_pypi import pypi_source

DATASET_NAME = "pypi_package_metadata"

# Specify exact (project, version) pairs or "project==version" strings
RELEASES = [
    ("cognee", "1.6.3"),
    ("packaging", "24.2"),
]


async def main() -> None:
    print(f"Ingesting PyPI releases into dataset '{DATASET_NAME}'...")
    for rel in RELEASES:
        print(f"  - {rel[0]}=={rel[1]}")

    # Build the PyPI dlt source
    source = pypi_source(RELEASES)

    # Ingest into cognee memory
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        max_rows_per_table=0,  # ingest all configured releases
    )

    print("\nQuerying ingested PyPI knowledge graph...")
    query = (
        "What Python version is required by cognee 1.6.3 "
        "and what are some of its core dependencies?"
    )
    answer = await cognee.recall(query, datasets=[DATASET_NAME])

    print(f"\nQuestion: {query}")
    print(f"Answer:\n{answer}")


if __name__ == "__main__":
    asyncio.run(main())
