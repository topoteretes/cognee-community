"""Ingest Prisma database schemas into cognee memory via the dlt connector.

Demonstrates:
1. Ingesting a ``schema.prisma`` file containing models, relations, and enums.
2. Building an AI agent knowledge graph with ``cognee.cognify()``.
3. Asking complex questions about database schema relationships and field types with ``cognee.search()``.
4. Demonstrating forget-on-delete / orphan cleanup when a model is removed from the schema.

Requires:
    pip install "cognee[dlt]"
    export LLM_API_KEY="your-llm-key"
"""

import asyncio
import os

import cognee
from cognee_community_connector_prisma import prisma_source

DATASET = "saas_database_schema"
CURRENT_DIR = os.path.dirname(__file__)
SCHEMA_PATH = os.path.join(CURRENT_DIR, "test_data", "schema.prisma")

QUESTION = "How is the Order model related to User, and what fields does it contain?"


async def main() -> None:
    print("1. Initializing clean memory state...")
    await cognee.forget(everything=True)

    print("2. Ingesting Prisma schema snapshot...")
    await cognee.remember(
        prisma_source(SCHEMA_PATH),
        dataset_name=DATASET,
        max_rows_per_table=0,  # Ingest entire schema snapshot
    )

    print("3. Building knowledge graph from database models & relations...")
    await cognee.cognify()

    print(f"\n4. Querying agent memory: '{QUESTION}'")
    answer = await cognee.search(QUESTION)
    print("\nAgent Answer:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
