"""ServiceNow connector demo — turn knowledge articles and incidents into memory.

Sync published knowledge articles plus incidents, problems and change requests into
cognee, then ask about them. Re-run after editing, resolving or deleting a record to
see the re-sync and forget-on-delete.

One-time setup (a free Personal Developer Instance works):

    export SERVICENOW_INSTANCE_URL=https://dev12345.service-now.com
    export SERVICENOW_USERNAME=...
    export SERVICENOW_PASSWORD=...
    export LLM_API_KEY=sk-...
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_servicenow import servicenow_source

# Keep ServiceNow in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "servicenow"


async def main() -> None:
    if not os.environ.get("SERVICENOW_INSTANCE_URL") or not os.environ.get("SERVICENOW_PASSWORD"):
        print(
            "Set SERVICENOW_INSTANCE_URL, SERVICENOW_USERNAME and SERVICENOW_PASSWORD to run this."
        )
        return

    include_journal = os.environ.get("SERVICENOW_INCLUDE_JOURNAL") == "1"
    print("Syncing ServiceNow into cognee ...")
    await cognee.remember(
        servicenow_source(include_journal=include_journal),
        dataset_name=DATASET_NAME,
        write_disposition="merge",
        self_improvement=False,
    )

    answer = await cognee.search(
        query_text="What recurring problems do the incidents show, and how were they fixed?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
