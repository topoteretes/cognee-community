"""Ingest a PubMed topic incrementally, then search it through cognee."""

import asyncio
import os

import cognee

from cognee_community_connector_pubmed import pubmed_source


async def main() -> None:
    source = pubmed_source(
        '"agentic"[Title/Abstract] AND 2026[Publication Date]',
        start_date="2026-01-01",
        api_key=os.getenv("NCBI_API_KEY"),
        email=os.getenv("NCBI_EMAIL"),
    )
    await cognee.remember(
        source,
        dataset_name="pubmed_agentic_ai",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    results = await cognee.search(
        query_text="Which agentic methods were evaluated in biomedical retrieval?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["pubmed_agentic_ai"],
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
