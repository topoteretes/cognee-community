"""PubMed connector demo — turn biomedical literature into AI memory.

Search PubMed, ingest the matching articles' metadata and abstracts into cognee, and ask
questions across them. ``pubmed_source`` returns a ``dlt`` source passed directly to
``cognee.remember``; each article becomes a Markdown document that goes through standard
cognify entity extraction (genes, diseases, methods, institutions, ...).

Re-running is incremental: only articles added to PubMed since the last run are fetched,
and articles that were deleted, retracted, or no longer match the query are forgotten.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. (Optional) Create an NCBI API key to raise the limit from 3 to 10 requests/second:
   sign in at https://account.ncbi.nlm.nih.gov/ -> Account settings ->
   "API Key Management" -> Create an API Key.

2. Export the key (optional), a contact email (recommended by NCBI) and your LLM key:
   export NCBI_API_KEY="your_ncbi_api_key"
   export NCBI_EMAIL="you@example.org"
   export LLM_API_KEY="sk-..."

3. Run this script:
   uv run python examples/example.py
"""

import asyncio

import cognee

from cognee_community_connector_pubmed import pubmed_source

DATASET_NAME = "pubmed_knowledge"


async def main() -> None:
    # `term` uses the same syntax as the PubMed search box. `mindate` keeps the first run
    # small for the demo; drop it to ingest every matching article.
    source = pubmed_source(term="CRISPR gene therapy", mindate="2026/01/01")

    print(f"Syncing PubMed articles into dataset '{DATASET_NAME}'...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )
    print("Ingestion & cognification complete!\n")

    queries = [
        "Which delivery methods are used for CRISPR gene therapies?",
        "Which diseases are being targeted with base or prime editing?",
    ]

    for q in queries:
        print(f"--- Query: {q} ---")
        results = await cognee.search(
            query_text=q,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=[DATASET_NAME],
        )
        print(f"Answer:\n{results}\n")


if __name__ == "__main__":
    asyncio.run(main())
