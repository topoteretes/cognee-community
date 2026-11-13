"""Airflow connector demo: put your DAGs into cognee memory and ask about them.

Setup:
    export AIRFLOW_URL=http://localhost:8080 AIRFLOW_USERNAME=admin AIRFLOW_PASSWORD=...
    export LLM_API_KEY=...            # plus LLM_MODEL / LLM_PROVIDER for non-OpenAI models
    uv run python examples/example.py

Re-run after editing, pausing or deleting a DAG to see the re-sync: edits are
picked up, paused DAGs stay, and removed (stale) DAGs are forgotten.
"""

import asyncio
import os

import cognee

from cognee_community_connector_airflow import airflow_source

DATASET_NAME = "airflow"


async def main() -> None:
    if not os.environ.get("AIRFLOW_URL"):
        print("Set AIRFLOW_URL and AIRFLOW_USERNAME/AIRFLOW_PASSWORD (or AIRFLOW_TOKEN).")
        return

    source = airflow_source(
        base_url=os.environ["AIRFLOW_URL"],
        token=os.environ.get("AIRFLOW_TOKEN"),
        username=os.environ.get("AIRFLOW_USERNAME"),
        password=os.environ.get("AIRFLOW_PASSWORD"),
        include_runs=True,
    )
    print("Syncing Airflow DAGs into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME, max_rows_per_table=0)

    for question in (
        "Which tasks run before publish_daily_revenue?",
        "Which DAG writes retail_demo.analytics.daily_revenue, and did its latest run succeed?",
    ):
        answer = await cognee.search(
            query_text=question,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=[DATASET_NAME],
        )
        print(f"\nQ: {question}\nA: {answer}")


if __name__ == "__main__":
    asyncio.run(main())
