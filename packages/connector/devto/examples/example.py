"""Example: Ingesting DEV.to Articles into Cognee AI Memory."""

import asyncio
import os
from cognee_community_connector_devto import (
    devto_source,
    fetch_devto_documents,
)

# Set your LLM provider key if not already exported
os.environ.setdefault("LLM_API_KEY", "your-llm-api-key")


async def run_document_ingestion_example():
    """Method A: Ingest DEV.to articles via cognee.add() and cognee.cognify()."""
    import cognee

    print("[1/3] Fetching latest DEV.to articles tagged with 'python'...")
    documents = fetch_devto_documents(tag="python", per_page=5, fetch_full_content=False)
    print(f"Fetched {len(documents)} articles formatted as documents.")

    print("\nSample Formatted Article Document:")
    print("------------------------------------------")
    print(documents[0][:400] + "\n...[truncated]")
    print("------------------------------------------")

    print("\n[2/3] Adding documents into Cognee dataset...")
    await cognee.add(documents, dataset_name="devto_developer_brain")

    print("[3/3] Cognifying knowledge into graph memory...")
    await cognee.cognify(datasets=["devto_developer_brain"])

    print("\nQuerying Cognee AI memory:")
    questions = [
        "What Python tutorials or libraries were discussed?",
        "Who wrote the featured articles and what topics do they cover?",
    ]
    for q in questions:
        print(f"\nQuestion: {q}")
        results = await cognee.search(q, datasets=["devto_developer_brain"])
        print(f"Answer / Results: {results}")


async def run_dlt_source_example():
    """Method B: Ingest structured DEV.to data via DLT source with cognee.remember()."""
    import cognee

    print("\nRunning Cognee remember pipeline with DEV.to DLT source...")
    source = devto_source(tag="ai", per_page=10)
    await cognee.remember(source, dataset_name="devto_dlt_memory")
    print("DLT ingestion completed.")


async def main():
    print("=== Cognee DEV.to Connector Demo ===")
    await run_document_ingestion_example()


if __name__ == "__main__":
    asyncio.run(main())
