"""Webflow connector demo — turn your Webflow CMS into memory you can ask questions of.

Syncs the CMS collections of a Webflow site (and optionally its static pages)
into cognee, then asks a question. Re-run it after editing, unpublishing or
deleting an item in Webflow: changed items are re-read through their
``lastUpdated`` stamp, and deleted or unpublished items are forgotten.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your published CMS items (and pages, if you ask for
them) and sends it, as text, to your configured LLM and embedding providers
during cognify. Select only what you mean to (``collections=``), keep drafts
off unless you want them, and use a dedicated dataset so you can forget it in
one call.

────────────────────────────────────────────────────────────────────────────
Setup
────────────────────────────────────────────────────────────────────────────
1. Install:  cd packages/connector/webflow && uv sync
2. Create a site token in Webflow: Site settings > Apps & integrations >
   API access > Generate API token, with the scopes ``sites:read`` and
   ``cms:read`` (add ``pages:read`` to also sync pages).
3. Export the token and your LLM key:

       export WEBFLOW_API_TOKEN="..."
       export LLM_API_KEY="sk-..."
       export WEBFLOW_COLLECTIONS="blog-posts,team"   # optional: slugs, ids or names
       export WEBFLOW_INCLUDE_PAGES=1                 # optional: needs pages:read

4. Run:  uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_webflow import webflow_source

# Keep Webflow in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "webflow"


async def main() -> None:
    if not os.getenv("WEBFLOW_API_TOKEN"):
        print("Set WEBFLOW_API_TOKEN to run this example (see its docstring).")
        return

    wanted = [name.strip() for name in os.getenv("WEBFLOW_COLLECTIONS", "").split(",")]
    source = webflow_source(
        collections=[name for name in wanted if name] or None,  # None = every collection
        include_pages=bool(os.getenv("WEBFLOW_INCLUDE_PAGES")),
    )

    print("Syncing Webflow into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # required: the source emits deletions
    )
    print("Sync stats:", source.cognee_sync_stats)

    answer = await cognee.search(
        query_text="What content is on this Webflow site, and what is it about?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit, unpublish or delete an item in Webflow, then re-run: changes re-sync and "
        "removed items are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
