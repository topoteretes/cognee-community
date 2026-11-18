import asyncio
import os

import cognee

from cognee_community_tasks_keenable import search_and_add, search_web


async def main():
    # cognee still needs an LLM key to ingest and query the results.
    os.environ["LLM_API_KEY"] = os.getenv("LLM_API_KEY", "YOUR_OPENAI_API_KEY")
    # Optional: set KEENABLE_API_KEY for the keyed endpoint and higher limits.
    # The search tasks also work without a key via the public endpoint.

    query = "How do knowledge graphs improve LLM memory?"

    print(f"Searching Keenable for: {query!r}")
    results = await search_web(query=query, num_results=5)
    for item in results:
        print(f"\nURL: {item['url']}")
        print(f"  Title: {item.get('title')}")
        content_preview = str(item["content"])[:300]
        print(f"  Content preview: {content_preview}...")

    print("\nSearching and adding results to cognee...")
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    await search_and_add(
        query=query,
        num_results=5,
        dataset_name="keenable_search",
    )

    search_results = await cognee.search("What is cognee?")
    print("\nSearch results after ingestion:")
    for result in search_results:
        print(f"  - {result}")


if __name__ == "__main__":
    asyncio.run(main())
