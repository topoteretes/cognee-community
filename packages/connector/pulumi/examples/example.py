"""Pulumi Cloud connector demo: turn stacks and their update history into memory.

``pulumi_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``.
Every stack and every finished update (kind, result, message, resource changes,
who ran it, the commit) is ingested as a normal document. Update ``config`` and
``environment`` are never read.

Re-running syncs only what changed: new finished updates are added and removed
stacks are forgotten together with their updates.

Privacy / opt-in
----------------
Stack history describes your infrastructure and who changed it. Nothing is fetched
until you run this script. Keep Pulumi in its own dataset.

One-time setup
--------------
Create an access token at https://app.pulumi.com (avatar, Personal access tokens),
then export it with the organization name and your LLM key:

    export PULUMI_ACCESS_TOKEN="pul-..."
    export PULUMI_ORG="acme"
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_pulumi import pulumi_source

DATASET_NAME = "pulumi"


async def main() -> None:
    required = ("PULUMI_ACCESS_TOKEN", "PULUMI_ORG")
    if not all(os.environ.get(name) for name in required):
        print(f"Set {', '.join(required)} to run this example.")
        return

    # Narrow the sync with projects=[...], tag_name=... or since_days=...
    source = pulumi_source(os.environ["PULUMI_ORG"], token=os.environ["PULUMI_ACCESS_TOKEN"])

    print("Syncing Pulumi Cloud into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        self_improvement=False,
    )
    print("Sync stats:", source.cognee_sync_stats)

    results = await cognee.recall(
        "Which stacks changed recently, and did any update fail?", datasets=[DATASET_NAME]
    )
    print("\nRecall result:\n", results)

    print(
        "\nRun `pulumi up` or remove a stack, then re-run: only finished new updates are "
        "added and removed stacks are forgotten with their updates."
    )


if __name__ == "__main__":
    asyncio.run(main())
