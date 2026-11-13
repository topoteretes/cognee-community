"""PostHog connector demo — turn product analytics into memory.

Sync a PostHog project's saved insights, dashboards, cohorts, feature flags and
experiments into cognee, then ask about them. Re-run after editing or deleting an
object in PostHog to see the incremental re-sync and forget-on-delete.

One-time setup:

    export POSTHOG_PROJECT_ID=12345
    export POSTHOG_API_KEY=phx_...                # personal API key with read scopes
    export POSTHOG_HOST=https://eu.posthog.com    # optional; US Cloud by default
    export LLM_API_KEY=sk-...
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_posthog import posthog_source

# Keep PostHog in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "posthog"


async def main() -> None:
    if not os.environ.get("POSTHOG_PROJECT_ID") or not os.environ.get("POSTHOG_API_KEY"):
        print("Set POSTHOG_PROJECT_ID and POSTHOG_API_KEY to run this.")
        return

    print("Syncing PostHog into cognee ...")
    source = posthog_source(
        project_id=os.environ["POSTHOG_PROJECT_ID"],
        api_key=os.environ["POSTHOG_API_KEY"],
        host=os.environ.get("POSTHOG_HOST", "https://us.posthog.com"),
    )
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        write_disposition="merge",
        self_improvement=False,
    )
    print("Sync stats:", source.cognee_sync_stats)

    for question in (
        "Which feature flags are rolled out, and to whom?",
        "What did our experiments conclude?",
    ):
        answer = await cognee.search(
            query_text=question,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=[DATASET_NAME],
        )
        print(f"\nQ: {question}\nA: {answer}")


if __name__ == "__main__":
    asyncio.run(main())
