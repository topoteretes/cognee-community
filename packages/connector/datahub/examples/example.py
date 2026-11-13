"""DataHub connector demo: put your catalog's context into cognee memory and ask about it.

Setup:
    export DATAHUB_GMS_URL=http://localhost:8080 DATAHUB_GMS_TOKEN=...
    export LLM_API_KEY=...            # plus LLM_MODEL / LLM_PROVIDER for non-OpenAI models
    uv run python examples/example.py

Re-run after changing an owner, tag or lineage edge, or soft-deleting a dataset,
to see the re-sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_datahub import datahub_source

DATASET_NAME = "datahub"


async def main() -> None:
    if not os.environ.get("DATAHUB_GMS_URL"):
        print("Set DATAHUB_GMS_URL and DATAHUB_GMS_TOKEN to run this example.")
        return

    source = datahub_source(
        base_url=os.environ["DATAHUB_GMS_URL"],
        token=os.environ.get("DATAHUB_GMS_TOKEN"),
        platforms=[p for p in os.environ.get("DATAHUB_PLATFORMS", "").split(",") if p] or None,
    )
    print("Syncing DataHub datasets into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME, max_rows_per_table=0)

    for question in (
        "Who owns the daily revenue dataset, and which domain is it in?",
        "Which datasets have columns tagged as PII?",
    ):
        answer = await cognee.search(
            query_text=question,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=[DATASET_NAME],
        )
        print(f"\nQ: {question}\nA: {answer}")


if __name__ == "__main__":
    asyncio.run(main())
