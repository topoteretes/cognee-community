"""Sync a Gong workspace and search its calls.

Set GONG_BASE_URL and either GONG_ACCESS_TOKEN or GONG_ACCESS_KEY plus
GONG_ACCESS_KEY_SECRET. Cognee also needs its normal LLM configuration.
"""

import asyncio
import os

import cognee

from cognee_community_connector_gong import gong_source


async def main() -> None:
    if not os.environ.get("GONG_BASE_URL"):
        raise SystemExit("Set GONG_BASE_URL to your tenant's API origin")
    source = gong_source(
        from_datetime=os.environ.get("GONG_FROM_DATETIME", "2026-01-01T00:00:00Z"),
        workspace_id=os.environ.get("GONG_WORKSPACE_ID") or None,
    )
    await cognee.remember(source, dataset_name="gong", write_disposition="merge")
    result = await cognee.search(
        query_text="What did customers ask for in recent calls?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["gong"],
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
