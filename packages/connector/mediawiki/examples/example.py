"""Sync selected MediaWiki pages into cognee and query them."""

import asyncio
import os

import cognee

from cognee_community_connector_mediawiki import mediawiki_source

DATASET_NAME = "mediawiki"


async def main() -> None:
    api_url = os.getenv("MEDIAWIKI_API_URL", "https://www.mediawiki.org/w/api.php")
    page_prefix = os.getenv("MEDIAWIKI_PAGE_PREFIX", "API:")

    source = mediawiki_source(
        api_url=api_url,
        page_prefix=page_prefix,
        namespaces=[0],
    )

    print(f"Syncing MediaWiki pages beginning with {page_prefix!r} ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    result = await cognee.search(
        query_text="Summarize the selected MediaWiki documentation.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
