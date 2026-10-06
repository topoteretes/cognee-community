"""Deel connector demo — sync organization workers and contracts into Cognee memory.

Pull Deel worker directories and contracts into Cognee with full forget-on-delete support.
`deel_source` returns a DLT source suitable for passing directly to `cognee.remember`.

────────────────────────────────────────────────────────────────────────────
Privacy & Sensitivity Note
────────────────────────────────────────────────────────────────────────────
Contract clauses and agreements can contain sensitive legal and financial data.
By default, the Deel connector operates in **metadata-first mode**: it ingests worker
directory profiles and sanitized contract summaries (title, worker, dates, status).
Full contract body clauses are strictly opt-in via:
    `include_contract_documents=True`

────────────────────────────────────────────────────────────────────────────
Setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector package:
       pip install "cognee-community-connector-deel"

2. Generate a Deel API Token from Deel Organization Settings:
   Organization Settings -> Apps & Integrations -> Developer Center -> Access Tokens

3. Export your tokens:
       export DEEL_API_TOKEN="your_deel_api_token"
       export LLM_API_KEY="your_openai_or_other_llm_key"

4. Run this example:
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_deel import deel_source

DATASET_NAME = "deel_hr"


async def main() -> None:
    token = os.environ.get("DEEL_API_TOKEN")
    if not token:
        print("Please export DEEL_API_TOKEN to run this example.")
        print("Usage: export DEEL_API_TOKEN='your_api_token'")
        return

    print("Configuring Deel source (metadata-first mode) ...")
    # Ingest worker directory and contracts metadata
    source = deel_source(
        token=token,
        include_workers=True,
        include_contracts=True,
        include_contract_documents=False,  # Privacy default
    )

    print(f"Syncing Deel organization data into Cognee dataset '{DATASET_NAME}' ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    print("Cognee memory sync complete!")

    # Search the ingested memory
    print("\nQuerying Cognee memory:")
    search_results = await cognee.search(
        "Who are our active software engineers and contractors?",
        dataset_name=DATASET_NAME,
    )
    print("Search results:", search_results)


if __name__ == "__main__":
    asyncio.run(main())
