"""Sync selected Pendo knowledge into cognee and search it.

Required environment variables:
    PENDO_INTEGRATION_KEY
    LLM_API_KEY

Optional:
    PENDO_CONTENT_TYPES=guides,feedback,nps
    PENDO_APP_ID=<Pendo application id>
    PENDO_NPS_SURVEY_IDS=<comma-separated Sentiment survey ids>
    PENDO_BASE_URL=https://app.eu.pendo.io  # regional subscriptions
"""

import asyncio
import os

import cognee

from cognee_community_connector_pendo import pendo_source

DATASET_NAME = "pendo"


def _csv_env(name: str, default: str = "") -> list[str]:
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


async def main() -> None:
    if not os.getenv("PENDO_INTEGRATION_KEY"):
        print("Set PENDO_INTEGRATION_KEY (and LLM_API_KEY) before running this example.")
        return

    source = pendo_source(
        content_types=_csv_env("PENDO_CONTENT_TYPES", "guides,feedback,nps"),
        nps_survey_ids=_csv_env("PENDO_NPS_SURVEY_IDS"),
    )

    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    result = await cognee.search(
        query_text="What themes appear in our product feedback and NPS comments?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
