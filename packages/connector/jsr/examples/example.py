"""Example demonstrating JSR connector ingestion into Cognee memory."""

import asyncio

import cognee

from cognee_community_connector_jsr import jsr_source


async def main() -> None:
    # Reset existing memory for demo
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    # Ingest JSR standard library packages into memory
    source = jsr_source(
        scopes=["std"],
        packages=["@std/path", "@std/fs"],
        include_docs=True,
    )

    await cognee.add(source)
    await cognee.cognify()

    # Query the knowledge graph about package utilities
    search_results = await cognee.search(
        "What path manipulation and filesystem functions are exported by @std/path and @std/fs?"
    )

    print("Search results from Cognee:")
    for result in search_results:
        print(f"- {result}")


if __name__ == "__main__":
    asyncio.run(main())
