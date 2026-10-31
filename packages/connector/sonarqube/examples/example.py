"""SonarQube connector demo — turn your code-quality state into cognee memory.

Sync SonarQube (or SonarCloud) issues, security hotspots, and quality-gate
outcomes. ``sonarqube_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``. Documents are ingested as normal documents (so they go
through the full cognify entity-extraction pipeline).

Each run is incremental: only issues created since the last sync are ingested,
and issues resolved upstream are reconciled out of memory (forget-on-delete).

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads only the projects your token can see. It is strictly opt-in —
nothing is fetched until you run this script — and documents go into a
dedicated dataset so you can wipe it with a single ``cognee.forget(dataset)``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector:

       cd packages/connector/sonarqube && uv sync

2. Generate a user token in SonarQube (My Account → Security) or SonarCloud.
3. Export your credentials and LLM key, then run:

       export SONARQUBE_TOKEN="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after new analysis results arrive to see the incremental sync; resolve
an issue upstream and re-run to see forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_sonarqube import sonarqube_source

# Keep code quality in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "code_quality"

# SonarCloud works too: base_url="https://sonarcloud.io"
BASE_URL = os.environ.get("SONARQUBE_URL", "https://sonarqube.example.com")

# None = every project the token can see. Prefer an explicit list for large
# servers, and set min_severity to bound each sync.
PROJECT_KEYS = None


async def main() -> None:
    if not os.environ.get("SONARQUBE_TOKEN"):
        print("Set SONARQUBE_TOKEN (and LLM_API_KEY) to run this example.")
        return

    print(f"Syncing SonarQube projects from {BASE_URL} into cognee ...")
    await cognee.remember(
        sonarqube_source(
            base_url=BASE_URL,
            project_keys=PROJECT_KEYS,
            min_severity="MAJOR",
        ),
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,  # unlimited read-back so deletions reconcile fully
    )

    answer = await cognee.search(
        query_text="What is blocking the quality gate and what are the worst findings?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nRe-run after new analysis results: only newly created issues are "
        "ingested, and issues resolved upstream are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
