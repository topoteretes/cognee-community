"""WordPress connector demo: turn a site's posts and pages into memory.

Pulls a WordPress site's posts and pages (with their comments) into cognee and
asks a question about them. ``wordpress_source`` returns a ``dlt`` source you
hand straight to ``cognee.remember``; every item becomes a document that goes
through cognee's normal entity extraction.

Run it again and only what changed on the site since the last run is synced:
edited items are re-ingested, and trashed, unpublished or deleted items are
forgotten.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
Public content needs no account. Nothing is fetched until you run this
script. Scope what you ingest with ``post_types``, ``categories`` or ``tags``,
and keep the site in its own dataset so you can drop it with
``cognee.forget``.

────────────────────────────────────────────────────────────────────────────
Run
────────────────────────────────────────────────────────────────────────────
    export LLM_API_KEY="sk-..."
    export WORDPRESS_URL="https://blog.example.com"   # any WordPress site
    # Optional, to include private posts: an Application Password
    # (Users → Profile → Application Passwords)
    # export WORDPRESS_USERNAME="editor" WORDPRESS_APP_PASSWORD="abcd efgh ..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_wordpress import wordpress_source

# Keep the site in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "wordpress"
SITE_URL = os.environ.get("WORDPRESS_URL", "https://wordpress.org/news")
# Your own site is synced in full (posts and pages). The default demo site has
# over a thousand posts, so the demo narrows it to one small category.
SCOPE = {} if os.environ.get("WORDPRESS_URL") else {"categories": ["documentation"]}
QUESTION = os.environ.get(
    "WORDPRESS_QUESTION", "How has WordPress improved its documentation over the years?"
)


async def main() -> None:
    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY to run this example.")
        return

    # Posts and pages by default; add custom post types with
    # post_types=["post", "page", "product"], or narrow posts with
    # categories=["news"] / tags=["release"].
    source = wordpress_source(SITE_URL, **SCOPE)

    print(f"Syncing {SITE_URL} into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # required: incremental upsert by item id
        max_rows_per_table=0,  # read back every synced item, not just 50
    )
    stats = source.cognee_sync_stats
    print(
        f"{stats['mode']} sync: {stats['items_changed']} item(s) ingested, "
        f"{stats['items_unchanged']} unchanged, {stats['deleted']} forgotten."
    )

    results = await cognee.recall(QUESTION, datasets=[DATASET_NAME])
    print("\nAnswer:", results[0].text if results else "(nothing found)")

    print(
        "\nRun this again later: only items changed on the site since this run are "
        "re-ingested, and deleted ones are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
