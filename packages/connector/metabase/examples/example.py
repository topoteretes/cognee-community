"""Sync Metabase knowledge into cognee, then search it.

Install this package, set METABASE_URL, METABASE_API_KEY and LLM_API_KEY,
then run: python examples/example.py. Alternatively set METABASE_USERNAME
and METABASE_PASSWORD instead of the API key. Re-run after edits or deletions.
"""

import asyncio
import os

import cognee

from cognee_community_connector_metabase import MetabaseUnchanged, metabase_source

DATASET_NAME = "metabase"


async def main() -> None:
    if not os.environ.get("METABASE_URL") or not (
        os.environ.get("METABASE_API_KEY")
        or (os.environ.get("METABASE_USERNAME") and os.environ.get("METABASE_PASSWORD"))
    ):
        print("Set METABASE_URL and METABASE_API_KEY (or METABASE_USERNAME/PASSWORD).")
        return

    print("Syncing Metabase collections, questions, and dashboards into cognee ...")
    try:
        await cognee.remember(metabase_source(), dataset_name=DATASET_NAME)
    except Exception as exc:
        # dlt and cognee wrap extraction exceptions. Suppress only the explicit
        # no-op signal; authentication, network, and ingestion errors propagate.
        cause = exc
        while cause is not None and not isinstance(cause, MetabaseUnchanged):
            cause = cause.__cause__
        if cause is None:
            raise
        print("Metabase content is unchanged.")
    answer = await cognee.search(
        query_text="Which questions and dashboards explain our business metrics?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)
    print("\nEdit or delete content in Metabase, then re-run to sync the changes.")


if __name__ == "__main__":
    asyncio.run(main())
