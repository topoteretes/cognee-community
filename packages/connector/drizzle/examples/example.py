"""Drizzle ORM connector demo: sync TypeScript database models into agent memory.

This example reads Drizzle ORM schema files (pgTable, mysqlTable, sqliteTable, relations, enums)
and indexes them into Cognee memory.

Once cognified, AI coding agents can answer structural database questions directly from memory:
- "What foreign keys point to the users table?"
- "What columns exist on the profiles table, and is bio nullable?"
- "What relations are defined on the posts model?"

Usage:
    export LLM_API_KEY="sk-..."
    python examples/example.py
"""

import asyncio
from pathlib import Path

import cognee

from cognee_community_connector_drizzle import drizzle_source

DATASET_NAME = "drizzle_schema_memory"


async def main() -> None:
    # 1. Point to your Drizzle schema file or schema directory
    fixtures_dir = Path(__file__).parent.parent / "tests" / "fixtures"
    schema_file = fixtures_dir / "postgres_schema.ts"

    if not schema_file.exists():
        print(f"Schema file not found at {schema_file}")
        return

    print(f"Loading Drizzle schema from: {schema_file}")

    # 2. Create the dlt source for Cognee
    source = drizzle_source(schema_path=schema_file)

    # 3. Ingest and cognify into Cognee knowledge graph
    print("Ingesting schema into Cognee memory...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    print("Running cognitive extraction (cognify)...")
    await cognee.cognify(datasets=[DATASET_NAME])

    # 4. Search and recall relational structure
    query = "What is the relation between users and posts in the schema?"
    print(f"\nQuerying Cognee: '{query}'")
    results = await cognee.search(query, dataset_name=DATASET_NAME)
    print("Result:", results)


if __name__ == "__main__":
    asyncio.run(main())
