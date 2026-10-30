"""Trigger.dev connector demo: ask your background jobs why they failed.

``triggerdev_source`` returns a ``dlt`` resource you hand straight to
``cognee.remember``. The first run reads the environment's schedules and the last
week of error groups, failed runs and deployments; later runs send only what
changed, and a deleted schedule is forgotten.

One-time setup
--------------
1. In the Trigger.dev dashboard open your project, pick the environment (prod,
   staging, dev), go to API keys and copy its secret key (``tr_prod_...``).
2. Export it, and an LLM key for cognee (e.g. ``LLM_API_KEY``)::

       export TRIGGER_SECRET_KEY="tr_prod_..."

Run it::

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_triggerdev import triggerdev_source

DATASET_NAME = "triggerdev"
# one resource_name per Trigger.dev environment; it scopes the sync state
RESOURCE_NAME = "triggerdev_prod"


async def sync(api_key: str) -> None:
    source = triggerdev_source(api_key=api_key, resource_name=RESOURCE_NAME)
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # required, the add pipeline defaults to "replace"
        max_rows_per_table=0,  # read the whole table back so deleted rows can be forgotten
        self_improvement=False,  # no whole-graph enrichment pass on every sync
    )
    print("sync stats:", source.cognee_sync_stats)


async def main() -> None:
    api_key = os.environ.get("TRIGGER_SECRET_KEY")
    if not api_key:
        print("Set TRIGGER_SECRET_KEY to an environment's secret key (see the docstring).")
        return

    await sync(api_key)

    answer = await cognee.search(
        query_text="Which tasks failed this week, and what error did each one hit?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Ops answer:", answer)

    # nothing changed upstream, so the second run sends nothing to cognee
    await sync(api_key)


if __name__ == "__main__":
    asyncio.run(main())
