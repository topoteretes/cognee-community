import asyncio
import os

import cognee

from cognee_community_tasks_firecrawl import scrape_and_add, scrape_urls, search_and_add


async def main():
    # Set required API keys
    os.environ["LLM_API_KEY"] = os.getenv("LLM_API_KEY", "YOUR_OPENAI_API_KEY")
    os.environ["FIRECRAWL_API_KEY"] = os.getenv("FIRECRAWL_API_KEY", "YOUR_FIRECRAWL_API_KEY")

    urls = ["https://docs.cognee.ai/"]

    # --- Example 1: scrape only ---
    print(f"Scraping {len(urls)} URL(s) with Firecrawl...")
    pages = await scrape_urls(urls)
    for page in pages:
        print(f"\nURL: {page['url']}")
        print(f"  Title: {page.get('title')}")
        print(f"  Markdown preview: {page['content'][:300]}...")

    # --- Example 2: scrape and add to cognee ---
    print("\nScraping and adding pages to cognee...")
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    await scrape_and_add(urls, dataset_name="firecrawl_example")

    # --- Example 3: search the web and add the result pages to cognee ---
    print("\nSearching and adding results to cognee...")
    await search_and_add(
        query="How do knowledge graphs improve LLM memory?",
        limit=3,
        dataset_name="firecrawl_search",
    )

    search_results = await cognee.search("What is cognee?")
    print("\nSearch results after ingestion:")
    for result in search_results:
        print(f"  - {result}")


if __name__ == "__main__":
    asyncio.run(main())
