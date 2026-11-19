import asyncio
import os

import cognee

from cognee_community_tasks_scrapegraph import scrape_and_add, scrape_urls


async def main():
    # Set required API keys
    os.environ["LLM_API_KEY"] = os.getenv("LLM_API_KEY", "YOUR_OPENAI_API_KEY")
    os.environ["SGAI_API_KEY"] = os.getenv("SGAI_API_KEY", "YOUR_SGAI_API_KEY")

    urls = [
        "https://example.com",
    ]

    # --- Example 1: scrape only ---
    print("Scraping URLs...")
    results = await scrape_urls(
        urls=urls,
        user_prompt="Extract the main content, title, and key information from this page",
    )
    for item in results:
        print(f"\nURL: {item['url']}")
        if item.get("error"):
            print(f"  Error: {item['error']}")
        else:
            content_preview = str(item["content"])[:300]
            print(f"  Content preview: {content_preview}...")

    # --- Example 2: scrape and add to cognee ---
    print("\nScraping and adding to cognee...")
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    await scrape_and_add(
        urls=urls,
        user_prompt="Extract the main content, title, and key information from this page",
        dataset_name="web_scrape",
    )

    search_results = await cognee.search("What is cognee?")
    print("\nSearch results after ingestion:")
    for result in search_results:
        print(f"  - {result}")


if __name__ == "__main__":
    asyncio.run(main())
