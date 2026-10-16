"""MediaWiki connector demo: turn wiki pages into memory, "ask my wiki".

Pulls a few Wikipedia pages into cognee and asks a question about them.
``mediawiki_source`` returns a ``dlt`` source you hand straight to
``cognee.remember``; every page becomes a document that goes through cognee's
normal entity extraction.

Run it again and only what changed on the wiki since the last run is synced:
edited pages are re-ingested, and deleted pages (or pages that left the
selection) are forgotten.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
Public wikis need no account. Nothing is fetched until you run this script.
Scope what you ingest with ``titles``, ``categories`` or ``namespaces``, and
keep the wiki in its own dataset so you can drop it with ``cognee.forget``.

────────────────────────────────────────────────────────────────────────────
Run
────────────────────────────────────────────────────────────────────────────
    export LLM_API_KEY="sk-..."
    # Optional: point at another wiki, e.g. your company's
    # export MEDIAWIKI_API_URL="https://wiki.example.com/w/api.php"
    # Optional, for a private wiki: a bot password from Special:BotPasswords
    # export MEDIAWIKI_USERNAME="You@cognee" MEDIAWIKI_PASSWORD="..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_mediawiki import mediawiki_source

# Keep the wiki in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "wiki"
API_URL = os.environ.get("MEDIAWIKI_API_URL", "https://en.wikipedia.org/w/api.php")
# Wikimedia asks API clients to identify themselves with contact details.
USER_AGENT = os.environ.get(
    "MEDIAWIKI_USER_AGENT",
    "cognee-mediawiki-example/0.1 (https://github.com/topoteretes/cognee-community)",
)


async def main() -> None:
    if not os.environ.get("LLM_API_KEY"):
        print("Set LLM_API_KEY to run this example.")
        return

    # Select explicit pages here; categories=[...] or namespaces=[...] work too.
    # On your own wiki, leave all three out to sync every article.
    source = mediawiki_source(
        API_URL,
        titles=["Ada Lovelace", "Charles Babbage", "Analytical engine"],
        user_agent=USER_AGENT,
    )

    print(f"Syncing pages from {API_URL} into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",  # required: incremental upsert by page id
        max_rows_per_table=0,  # read back every synced page, not just 50
    )
    stats = source.cognee_sync_stats
    print(
        f"{stats['mode']} sync: {stats['pages_changed']} page(s) ingested, "
        f"{stats['pages_unchanged']} unchanged, {stats['deleted']} forgotten."
    )

    results = await cognee.recall(
        "What did Ada Lovelace write about Babbage's analytical engine?",
        datasets=[DATASET_NAME],
    )
    print("\nAnswer:", results[0].text if results else "(nothing found)")

    print(
        "\nRun this again later: only pages edited on the wiki since this run are "
        "re-ingested, and deleted pages are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
