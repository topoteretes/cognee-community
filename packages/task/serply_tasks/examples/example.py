import asyncio
import os

import cognee

from cognee_community_tasks_serply import search_and_add, search_web


async def main():
    # Set required API keys
    os.environ["LLM_API_KEY"] = os.getenv("LLM_API_KEY", "YOUR_OPENAI_API_KEY")
    os.environ["SERPLY_API_KEY"] = os.getenv("SERPLY_API_KEY", "YOUR_SERPLY_API_KEY")

    query = "How do knowledge graphs improve LLM memory?"

    # --- Example 1: search only ---
    print(f"Searching Google with Serply for: {query!r}")
    results = await search_web(query=query, num_results=5)
    for item in results:
        print(f"\nURL: {item['url']}")
        print(f"  Title: {item.get('title')}")
        print(f"  Snippet: {item['description']}")

    # --- Example 2: search and add the result pages to cognee ---
    print("\nSearching and adding results to cognee...")
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    await search_and_add(
        query=query,
        num_results=3,
        dataset_name="serply_search",
    )

    search_results = await cognee.search("How do knowledge graphs help LLM memory?")
    print("\nSearch results after ingestion:")
    for result in search_results:
        print(f"  - {result}")


if __name__ == "__main__":
    asyncio.run(main())
