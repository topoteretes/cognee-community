"""Example script showing Snowflake ingestion and search in cognee.

This script demonstrates:
1. Connecting with Snowflake Key-Pair authentication.
2. Ingesting table and column comments into the document path (searchable via LLM).
3. Ingesting tabular rows incrementally via Snowflake CHANGES / timestamp cursor.
4. Ingesting explicit custom queries into relational memory.
5. Searching for ingested concepts using cognee.search().
"""

import asyncio
import os

import cognee

from cognee_community_connector_snowflake import snowflake_source


async def main():
    print("=== Cognee Snowflake Connector Example ===")

    account = os.environ.get("SNOWFLAKE_ACCOUNT", "xy12345.us-east-1")
    user = os.environ.get("SNOWFLAKE_USER", "COGNEE_USER")
    key_file = os.environ.get("SNOWFLAKE_PRIVATE_KEY_FILE", "rsa_key.p8")
    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    warehouse = os.environ.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")
    database = os.environ.get("SNOWFLAKE_DATABASE", "ANALYTICS")
    schema = os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC")

    # 1. Define tables to sync
    tables = [
        {
            "database": database,
            "schema": schema,
            "table": "CUSTOMERS",
            "primary_key": "CUSTOMER_ID",
            "use_changes": True,  # Uses CHANGES(INFORMATION => DEFAULT) if enabled
            "cursor_column": "UPDATED_AT",  # Fallback cursor
        }
    ]

    # 2. Define explicit read-only queries
    queries = [
        {
            "name": "high_value_customers",
            "sql": (
                f"SELECT CUSTOMER_ID, NAME, TIER FROM {database}.{schema}.CUSTOMERS "
                "WHERE TIER = 'ENTERPRISE'"
            ),
            "primary_key": "CUSTOMER_ID",
        }
    ]

    # 3. Create the dlt source
    source = snowflake_source(
        account=account,
        user=user,
        private_key_file=key_file,
        private_key_passphrase=passphrase,
        warehouse=warehouse,
        database=database,
        schema=schema,
        tables=tables,
        queries=queries,
        include_comments=True,
    )

    # 4. Add data to Cognee and extract knowledge graph
    print("Ingesting Snowflake data and metadata into cognee...")
    await cognee.add(source)
    await cognee.cognify()

    # 5. Search the extracted memory
    query = "What columns exist in the CUSTOMERS table?"
    print(f"\nSearching for: '{query}'")
    results = await cognee.search(query)
    for res in results:
        print(f"- {res}")


if __name__ == "__main__":
    asyncio.run(main())
