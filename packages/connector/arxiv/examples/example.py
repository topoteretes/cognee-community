"""Ingest a small arXiv query snapshot into a dedicated cognee dataset."""

import asyncio

import cognee

from cognee_community_connector_arxiv import arxiv_source


async def main() -> None:
    source = arxiv_source(
        categories=["cs.AI"],
        submitted_date_range=("202601010000", "202601072359"),
    )
    await cognee.remember(
        source,
        dataset_name="arxiv-cs-ai-week",
        write_disposition="replace",
        max_rows_per_table=0,
    )
    print("Completed the arXiv snapshot. Search dataset 'arxiv-cs-ai-week' in cognee.")


if __name__ == "__main__":
    asyncio.run(main())
