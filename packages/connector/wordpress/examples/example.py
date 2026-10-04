"""WordPress connector demo — "ask my WordPress site".

Pull WordPress posts, pages, and comments into cognee memory, incrementally,
with forget-on-delete.

This example builds on cognee's DLT ingestion subsystem and document-mode routing.
``wordpress_source`` produces a ``dlt`` resource that you pass directly to
``cognee.remember``. The first run backfills the selected content types; re-running
``remember`` syncs only content modified since the previous run, and posts or pages
deleted upstream are removed from memory on the subsequent sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install dependencies:
       pip install cognee dlt requests

2. Generate a WordPress Application Password:
   - Log into your WordPress admin dashboard (e.g. https://your-site.com/wp-admin).
   - Navigate to Users -> Profile.
   - Scroll down to "Application Passwords".
   - Enter a name (e.g., "cognee-connector") and click "Add New Application Password".
   - Copy the generated password.

3. Set your environment variables:
       export WORDPRESS_URL="https://your-site.com"
       export WORDPRESS_USERNAME="your-username"
       export WORDPRESS_APP_PASSWORD="xxxx xxxx xxxx xxxx"
       export LLM_API_KEY="your-llm-key"

4. Run the script:
       python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_wordpress import wordpress_source

# Isolate the content in a dedicated dataset
DATASET_NAME = "wordpress_site"

# Settings for cognee.remember()
WORDPRESS_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def main():
    base_url = os.environ.get("WORDPRESS_URL")
    username = os.environ.get("WORDPRESS_USERNAME")
    app_password = os.environ.get("WORDPRESS_APP_PASSWORD") or os.environ.get("WORDPRESS_API_KEY")

    if not all([base_url, username, app_password]):
        print(
            "Please set WORDPRESS_URL, WORDPRESS_USERNAME, and WORDPRESS_APP_PASSWORD.\n"
            "Refer to the instructions at the top of this file and re-run."
        )
        return

    # Clean previous demo state
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    def build_source():
        return wordpress_source(
            base_url=base_url,
            username=username,
            app_password=app_password,
            content_types=["posts", "pages", "comments"],
        )

    # ── First sync: full backfill ──────────────────────────────────────────
    print("\n=== WordPress sync #1 (backfill) ===")
    result = await cognee.remember(
        build_source(),
        dataset_name=DATASET_NAME,
        **WORDPRESS_REMEMBER_KWARGS,
    )
    print("Backfill result:", result)

    # ── Search content in memory ───────────────────────────────────────────
    answer = await cognee.search(
        query_text="What are the recent posts and articles about?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nWordPress summary:\n", answer)

    # ── Second sync: incremental delta + forget-on-delete ──────────────────
    print("\n=== WordPress sync #2 (incremental update) ===")
    result = await cognee.remember(
        build_source(),
        dataset_name=DATASET_NAME,
        **WORDPRESS_REMEMBER_KWARGS,
    )
    print("Incremental sync result:", result)


if __name__ == "__main__":
    asyncio.run(main())
