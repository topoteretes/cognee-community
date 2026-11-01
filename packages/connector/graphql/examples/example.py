"""Ingest GraphQL SDL schemas into cognee memory via dlt.

Demonstrates:
1. Ingesting a ``schema.graphql`` file containing types, queries, mutations, and enums.
2. Building an AI agent knowledge graph with ``cognee.cognify()``.
3. Asking complex questions about GraphQL queries, inputs, and mutations with ``cognee.search()``.
4. Demonstrating forget-on-delete / orphan cleanup when an operation is removed from the schema.

Requires:
    pip install "cognee[dlt]"
    export LLM_API_KEY="your-llm-key"
"""

import asyncio
import os

import cognee
from cognee_community_connector_graphql import graphql_source

DATASET = "graphql_api_schema"
CURRENT_DIR = os.path.dirname(__file__)
SCHEMA_PATH = os.path.join(CURRENT_DIR, "test_data", "schema.graphql")

QUESTION = "What are the required input fields for the createArticle mutation, and what does it return?"


async def main() -> None:
    print("1. Initializing clean memory state...")
    await cognee.forget(everything=True)

    print("2. Ingesting GraphQL SDL schema snapshot...")
    await cognee.remember(
        graphql_source(SCHEMA_PATH),
        dataset_name=DATASET,
        max_rows_per_table=0,  # Ingest entire schema snapshot
    )

    print("3. Building knowledge graph from GraphQL types & operations...")
    await cognee.cognify()

    print(f"\n4. Querying agent memory: '{QUESTION}'")
    answer = await cognee.search(QUESTION)
    print("\nAgent Answer:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
