"""BambooHR connector demo — "ask my HR system".

Sync BambooHR employees and company files into cognee memory, incrementally,
with forget-on-delete.

``bamboohr_source`` returns a dlt source you hand straight to
``cognee.remember``. The first run backfills every employee and readable
company file; re-running syncs only employees changed since the last run, and
employees deleted (or terminated) in BambooHR are forgotten.

One-time setup
--------------
1. In BambooHR, click your name (lower left) → API Keys → Add New Key.
2. Export your connection details:

       BAMBOOHR_COMPANY_DOMAIN=acme     # from https://acme.bamboohr.com
       BAMBOOHR_API_KEY=...

3. Configure cognee's LLM + embeddings in ``.env`` like any other cognee run.

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_bamboohr import bamboohr_source

DATASET_NAME = "bamboohr"

# "merge" is required: it is what makes re-runs incremental and what lets the
# _deleted markers remove employees. cognee's default ("replace") would drop
# every employee that did not change since the last run.
REMEMBER_KWARGS = {"primary_key": "id", "write_disposition": "merge"}


async def main() -> None:
    if not (os.environ.get("BAMBOOHR_COMPANY_DOMAIN") and os.environ.get("BAMBOOHR_API_KEY")):
        print("Set BAMBOOHR_COMPANY_DOMAIN and BAMBOOHR_API_KEY (see this file's docstring).")
        return

    print("=== Sync #1 (backfill) ===")
    result = await cognee.remember(bamboohr_source(), dataset_name=DATASET_NAME, **REMEMBER_KWARGS)
    print(result)

    answer = await cognee.search(
        query_text="Who works in which department, and what are their job titles?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Answer:", answer)

    # Re-running against the same dataset reuses the saved cursor: only
    # employees changed since sync #1 are fetched, and deleted ones are
    # forgotten.
    print("\n=== Sync #2 (incremental) ===")
    result = await cognee.remember(bamboohr_source(), dataset_name=DATASET_NAME, **REMEMBER_KWARGS)
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
