"""Sync selected Braze content into cognee and search it.

Required environment variables:

    BRAZE_API_KEY="..."
    BRAZE_REST_ENDPOINT="https://rest.iad-01.braze.com"
    LLM_API_KEY="..."

Run from packages/connector/braze with:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_braze import braze_source

DATASET_NAME = "braze"
REMEMBER_OPTIONS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


def build_source():
    # Choose either or both families. Add campaign_ids=(...) or canvas_ids=(...)
    # to limit ingestion to an explicit allowlist.
    return braze_source(content_types=("campaigns", "canvases"))


async def main() -> None:
    if not os.environ.get("BRAZE_API_KEY"):
        print("Set BRAZE_API_KEY before running this example.")
        return
    if not (os.environ.get("BRAZE_REST_ENDPOINT") or os.environ.get("BRAZE_CLUSTER")):
        print("Set BRAZE_REST_ENDPOINT (recommended) or BRAZE_CLUSTER.")
        return

    print("Syncing Braze campaigns and Canvases...")
    await cognee.remember(
        build_source(),
        dataset_name=DATASET_NAME,
        **REMEMBER_OPTIONS,
    )

    result = await cognee.search(
        query_text="Summarize our active lifecycle messaging and its key calls to action.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", result)

    print(
        "\nRe-run this script after editing or removing an upstream item. "
        "Only changed details are fetched, and removed items are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
