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

# Keep Deel organization data in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "deel_hr"


async def main() -> None:
    token = os.environ.get("DEEL_API_TOKEN")
    if not token:
        print("Please export DEEL_API_TOKEN to run this example.")
        print("Usage: export DEEL_API_TOKEN='your_api_token'")
        return

    # Start from a clean slate so the demo is reproducible.
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    # ── First sync: full backfill ──────────────────────────────────────────
    print("\n=== Deel sync #1 (backfill) ===")
    source = deel_source(
        token=token,
        include_workers=True,
        include_contracts=True,
        include_contract_documents=False,  # Privacy default: metadata only
    )

    print(f"Syncing Deel organization data into Cognee dataset '{DATASET_NAME}' ...")
    result = await cognee.remember(source, dataset_name=DATASET_NAME)
    print("Sync #1 result:", result)

    answer = await cognee.search(
        query_text="Who are our active software engineers and team members?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nWorkforce summary:\n", answer)

    # ── Second sync: incremental delta + forget-on-delete ──────────────────
    print("\n=== Deel sync #2 (incremental + forget-on-delete) ===")
    source = deel_source(
        token=token,
        include_workers=True,
        include_contracts=True,
        include_contract_documents=False,
    )
    result = await cognee.remember(source, dataset_name=DATASET_NAME)
    print("Sync #2 result:", result)

    answer = await cognee.search(
        query_text="What changed in our contracts or active workforce?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nUpdated workforce query:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
