"""Vercel connector demo: ask memory which deploy broke and why.

Pull Vercel projects, deployments and failed-build output into cognee.
``vercel_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``. Rows are ingested as normal documents, so they go through
the full cognify entity-extraction pipeline.

Each run is a full snapshot of the last ``lookback_days``. Unchanged rows keep a
stable id and are not re-cognified. Deployments you delete in Vercel, or that
age out of the window, drop out of the snapshot and are forgotten on the next
sync.

Privacy / opt-in
----------------
This reads project settings, deployment metadata and the build output of failed
deployments. Environment variable values, deploy-hook URLs and protection
secrets are never ingested. Build output is ingested as Vercel returns it, so
pass ``include_build_logs=False`` if your builds print secrets. Use a dedicated
dataset so you can wipe it on its own.

One-time setup
--------------
1. Install the connector:

       uv pip install cognee-community-connector-vercel
       # or, from this monorepo: cd packages/connector/vercel && uv sync

2. Create an access token at https://vercel.com/account/tokens. Scope it to one
   team or project and give it an expiry.
3. Export the token and your LLM key, then run:

       export VERCEL_TOKEN="vcp_..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after a new deployment, or after deleting one, to see the re-sync.
"""

import asyncio
import os

import cognee

from cognee_community_connector_vercel import vercel_source

# Keep Vercel in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "vercel"


async def main() -> None:
    if not os.environ.get("VERCEL_TOKEN"):
        print("Set VERCEL_TOKEN to run this example.")
        return

    # Scope with project_ids=[...] and lookback_days=...; the defaults read every
    # project the token can see and the last 30 days of deployments.
    source = vercel_source(lookback_days=7)

    print("Syncing Vercel projects and deployments into cognee ...")
    # self_improvement=False: a re-sync should not trigger the whole-graph
    # enrichment pass every time.
    await cognee.remember(source, dataset_name=DATASET_NAME, self_improvement=False)

    answer = await cognee.search(
        query_text="Which deployments failed, and what did the build output say?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nDeploy again or delete a deployment in Vercel, then re-run: changes re-sync "
        "and removed deployments are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
