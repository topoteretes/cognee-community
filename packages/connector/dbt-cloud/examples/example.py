"""dbt Cloud connector demo — turn your dbt project into memory.

Pull dbt Cloud model/source/exposure/metric documentation, lineage, and job
run outcomes into cognee. ``dbt_cloud_source`` returns a ``dlt`` source you
hand to ``cognee.remember``/``cognee.add`` with ``write_disposition="merge"``
and ``max_rows_per_table=0`` (required for forget-on-delete — see below).
Definitions and run outcomes are ingested as normal documents (so they go
through the full cognify entity-extraction pipeline, unlike the relational
dlt connectors).

The first sync is a full pass: the manifest for each selected environment's
latest successful run is parsed directly for definitions (lineage included
as text), and each selected job's newest runs become run-outcome documents.
Re-running this script later does an incremental sync — only new runs and
changed manifests are re-fetched — and forgets anything that no longer
exists upstream: a model removed from the project, a run that aged out of
the window or was deleted in dbt Cloud, or a job/environment no longer in
scope. See the README's "How sync + forget-on-delete work" for the full
full-pass/incremental-pass and safety-guard design.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads your dbt project's model descriptions, lineage, and run history
(including failure messages). It is strictly opt-in — nothing is fetched
until you run this script. Scope what you ingest with `environment_ids` /
`project_ids` / `job_ids`, and use a dedicated dataset so you can wipe it
with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Start a dbt Cloud/platform trial (or use an existing Starter/Enterprise
   account) and note your account id and access-URL host from Account
   settings -> Account information.
2. Create a personal access token with read-only access to Jobs, Runs, and
   Artifacts (see the README's "Minimum permissions" section).
3. Export the credential and your LLM key, then run:

       export DBT_CLOUD_ACCOUNT_ID="12345"
       export DBT_CLOUD_API_TOKEN="..."
       export DBT_CLOUD_HOST="abc123.us1.dbt.com"
       export DBT_CLOUD_ENVIRONMENT_IDS="67890"
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

   Set exactly one of DBT_CLOUD_ENVIRONMENT_IDS / DBT_CLOUD_PROJECT_IDS /
   DBT_CLOUD_JOB_IDS (comma-separated ids); selection is required.
"""

import asyncio
import os

import cognee

from cognee_community_connector_dbt_cloud import dbt_cloud_source

# Keep dbt Cloud in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "dbt_cloud"


def _parse_ids(value: str | None) -> list[int] | None:
    if not value:
        return None
    return [int(piece.strip()) for piece in value.split(",") if piece.strip()]


async def main() -> None:
    account_id = os.environ.get("DBT_CLOUD_ACCOUNT_ID")
    if not account_id or not os.environ.get("DBT_CLOUD_API_TOKEN"):
        print("Set DBT_CLOUD_ACCOUNT_ID and DBT_CLOUD_API_TOKEN to run this example.")
        return

    environment_ids = _parse_ids(os.environ.get("DBT_CLOUD_ENVIRONMENT_IDS"))
    project_ids = _parse_ids(os.environ.get("DBT_CLOUD_PROJECT_IDS"))
    job_ids = _parse_ids(os.environ.get("DBT_CLOUD_JOB_IDS"))
    if not (environment_ids or project_ids or job_ids):
        print(
            "Set one of DBT_CLOUD_ENVIRONMENT_IDS, DBT_CLOUD_PROJECT_IDS, or DBT_CLOUD_JOB_IDS "
            "(comma-separated ids) to run this example."
        )
        return

    source = dbt_cloud_source(
        account_id=account_id,
        environment_ids=environment_ids,
        project_ids=project_ids,
        job_ids=job_ids,
    )

    print("Syncing dbt Cloud definitions and run outcomes into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="Summarize the models in this project and whether their latest runs passed.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nRemove a model from the project, let a job run again, or let a run fail, then "
        "re-run this script: the incremental sync picks up what changed and forgets what's "
        "gone."
    )


if __name__ == "__main__":
    asyncio.run(main())
