"""Example demonstrating how to ingest Braintrust AI evaluations into Cognee."""

import asyncio
import os
import cognee
from cognee_community_connector_braintrust import braintrust_source


async def main() -> None:
    # 1. Initialize Braintrust connector
    source = braintrust_source(
        api_key=os.getenv("BRAINTRUST_API_KEY", "test_key"),
        project_names=["rag-eval-prod"],
        include_prompts=True,
        include_datasets=True,
    )

    # 2. Add to Cognee memory
    print("Ingesting Braintrust AI evaluations, experiments, and prompts into Cognee...")
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    print("Running Cognify extraction pipeline...")
    await cognee.cognify()

    # 4. Search LLMOps intelligence
    query = "Which experiments had the highest RAG accuracy scores, and which prompt template was used?"
    print(f"\nSearching Cognee memory: '{query}'")
    results = await cognee.search(query)
    for result in results:
        print(f"- {result}")


if __name__ == "__main__":
    asyncio.run(main())
