"""Google Search Console connector demo — turn your search performance into memory.

Syncs the last few months of one or more Search Console properties into cognee, then asks a
question. Re-run it later: closed months are not read again, the open month is refreshed, and a
property you remove or deselect is forgotten.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads your sites' search queries, pages and click data and sends it, as text, to your
configured LLM and embedding providers during cognify. Select only the properties you mean to
(``GOOGLE_SEARCH_CONSOLE_SITES``) and use a dedicated dataset so you can forget it in one call.

────────────────────────────────────────────────────────────────────────────
Setup
────────────────────────────────────────────────────────────────────────────
1. Install:  cd packages/connector/google-search-console && uv sync
2. In Google Cloud, enable the "Google Search Console API" and create an OAuth client of type
   "Desktop app". Download its JSON. (README: "Setup".)
3. Export the client file and your LLM key:

       export GOOGLE_SEARCH_CONSOLE_CREDENTIALS_PATH=~/client_secret.json
       export LLM_API_KEY="sk-..."
       export GOOGLE_SEARCH_CONSOLE_SITES="sc-domain:example.com"   # optional; default: all

4. Sign in once:  uv run python examples/login.py
5. Run:           uv run python examples/example.py ["optional question"]
"""

import asyncio
import os
import sys

import cognee

from cognee_community_connector_google_search_console import (
    google_search_console_source,
    list_properties,
)

# Keep Search Console in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "search_console"


async def main() -> None:
    properties = list_properties()
    print("Properties this account can see:")
    for entry in properties:
        print(f"  {entry['siteUrl']}  ({entry['permissionLevel']})")

    wanted = [name.strip() for name in os.getenv("GOOGLE_SEARCH_CONSOLE_SITES", "").split(",")]
    source = google_search_console_source(
        sites=[name for name in wanted if name] or None,  # None = every verified property
        months=3,  # this month and the two before it
    )

    print("\nSyncing Search Console into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # required: incremental runs emit changes and deletions
    )
    # Counts only, never queries or pages.
    print("Sync stats:", source.cognee_sync_stats)

    question = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "Which search queries bring the most clicks, and to which pages?"
    )
    answer = await cognee.recall(question, datasets=[DATASET_NAME])
    print("\nRecall:\n", answer)

    print(
        "\nRun it again later: closed months are skipped, the open month is refreshed, and "
        "a property you remove or deselect is forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
