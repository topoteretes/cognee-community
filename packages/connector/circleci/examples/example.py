"""CircleCI connector demo: turn your CI history into memory.

Setup:

    export CIRCLECI_TOKEN="..."                      # User Settings → Personal API Tokens
    export CIRCLECI_PROJECT_SLUG="gh/<org>/<repo>"   # Project Settings → Overview
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_circleci import circleci_source

DATASET_NAME = "circleci"


async def main() -> None:
    slug = os.environ.get("CIRCLECI_PROJECT_SLUG")
    if not (os.environ.get("CIRCLECI_TOKEN") and slug):
        print("Set CIRCLECI_TOKEN and CIRCLECI_PROJECT_SLUG to run this example.")
        return

    await cognee.remember(
        circleci_source(project_slugs=[slug]),
        dataset_name=DATASET_NAME,
        primary_key="id",
        # "merge" keeps earlier pipelines. The default ("replace") would forget
        # every pipeline that isn't new on this run.
        write_disposition="merge",
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="Which tests failed most recently, and why?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
