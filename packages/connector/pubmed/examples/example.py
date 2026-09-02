"""Ingest a PubMed topic selection into a dedicated cognee dataset."""

import asyncio

import cognee

from cognee_community_connector_pubmed import pubmed_source


async def main() -> None:
    source = pubmed_source('"traditional Chinese medicine"')
    await cognee.remember(
        source,
        dataset_name="pubmed",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


if __name__ == "__main__":
    asyncio.run(main())
