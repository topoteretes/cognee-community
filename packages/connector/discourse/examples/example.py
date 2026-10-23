"""Discourse connector demo — turn forum threads into memory.

Pull Discourse topics into cognee, with forget-on-delete. ``discourse_source``
returns a ``dlt`` source you hand straight to ``cognee.remember`` — no routing
kwargs needed. Each topic (opening post + replies) is ingested as one normal
document, so it goes through the full cognify entity-extraction pipeline.

Each run is a full snapshot: unchanged topics keep a stable id and are not
re-cognified, topics deleted on the forum drop out of the snapshot (so cognee's
orphan cleanup forgets them), and incremental sync only re-downloads topics
that got new activity since the last run.

────────────────────────────────────────────────────────────────────────────
Scope / politeness
────────────────────────────────────────────────────────────────────────────
This reads a public forum by default (meta.discourse.org, one small category).
Unscoped, the connector ingests the *whole* forum, and a parent category also
includes all of its subcategories, so check the size of your scope first. Keep
``request_delay`` on forums you don't run, and use a dedicated dataset so you can
wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       cd packages/connector/discourse && uv sync

2. Export your LLM key (any provider cognee supports), then run:

       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

   Optional: point it at your own forum with DISCOURSE_BASE_URL, and for a
   private forum also set DISCOURSE_API_KEY and DISCOURSE_API_USERNAME.

Re-run to see the incremental sync: topics without new activity are served from
the cache instead of being downloaded again.
"""

import asyncio
import os

import cognee

from cognee_community_connector_discourse import discourse_source

# Keep Discourse in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "discourse"

# meta.discourse.org's "comparison" category: ~12 threads comparing Discourse to
# other community tools. Override with DISCOURSE_BASE_URL / DISCOURSE_CATEGORY_ID.
DEFAULT_FORUM = "https://meta.discourse.org"
DEFAULT_CATEGORY_ID = 63


async def main() -> None:
    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY to run this example (cognify needs an LLM).")
        return

    base_url = os.environ.get("DISCOURSE_BASE_URL", DEFAULT_FORUM)
    category_id = int(os.environ.get("DISCOURSE_CATEGORY_ID", DEFAULT_CATEGORY_ID))

    source = discourse_source(
        base_url,
        category_ids=[category_id],
        # Be polite to forums you don't run.
        request_delay=0.5,
    )

    print(f"Syncing category {category_id} of {base_url} into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Summarize what these forum threads discuss.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nRe-run to see incremental sync: unchanged topics are reused, topics with "
        "new replies are re-fetched, and deleted topics are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
