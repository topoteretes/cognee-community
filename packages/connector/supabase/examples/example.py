"""Minimal Supabase → cognee sync.

Required environment variables:
  SUPABASE_DATABASE_URL  PostgreSQL URL for a dedicated read-only role
  SUPABASE_PROJECT_REF   Stable project reference from the Supabase dashboard
  LLM_API_KEY            Your configured cognee model provider key
"""

import asyncio
import os

import cognee

from cognee_community_connector_supabase import supabase_source


async def main() -> None:
    source = supabase_source(
        os.environ["SUPABASE_DATABASE_URL"],
        project_ref=os.environ["SUPABASE_PROJECT_REF"],
        tables=["customers"],
        columns={"customers": ["id", "name", "company", "updated_at"]},
        cursor_columns={"customers": "updated_at"},
    )
    await cognee.remember(
        source,
        dataset_name="supabase_customers",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    results = await cognee.search(
        query_text="Which customers were updated most recently?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["supabase_customers"],
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
