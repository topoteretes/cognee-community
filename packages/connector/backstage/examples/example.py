"""Backstage connector demo — turn a software catalog into memory.

Sync a Backstage catalog (components, APIs, systems, groups, users) into cognee,
then ask who owns what. Re-run after changing or removing catalog entities to see
the incremental re-sync and forget-on-delete.

Works out of the box against the public demo instance, which allows guest reads:

    export LLM_API_KEY=sk-...
    uv run python examples/example.py

For your own instance, set BACKSTAGE_BASE_URL and, if it needs auth, BACKSTAGE_TOKEN
(a static external-access token).
"""

import asyncio
import os

import cognee

from cognee_community_connector_backstage import backstage_source

# Keep Backstage in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "backstage"


async def main() -> None:
    base_url = os.environ.get("BACKSTAGE_BASE_URL", "https://demo.backstage.io")
    print(f"Syncing the Backstage catalog at {base_url} into cognee ...")
    await cognee.remember(
        backstage_source(
            base_url=base_url,
            token=os.environ.get("BACKSTAGE_TOKEN"),
            # TechDocs are opt-in, per entity ("kind:namespace/name").
            techdocs=["component:default/backstage-demo"]
            if "demo.backstage.io" in base_url
            else [],
        ),
        dataset_name=DATASET_NAME,
        write_disposition="merge",
        self_improvement=False,
    )

    for question in (
        "Which team owns the queue-proxy component, and who is on that team?",
        "Which components are part of the podcast system?",
    ):
        answer = await cognee.search(
            query_text=question,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=[DATASET_NAME],
        )
        print(f"\nQ: {question}\nA: {answer}")


if __name__ == "__main__":
    asyncio.run(main())
