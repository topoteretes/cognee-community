"""GitHub connector demo: turn your GitHub repositories into memory.

Pulls repositories (description, topics, README), issues, pull requests and
releases into cognee, incrementally and with forget-on-delete.
``github_source`` returns a ``dlt`` source you hand to ``cognee.remember``.
Rows are ingested as normal documents, so they go through the full cognify
entity-extraction pipeline.

Run it twice. The first run ingests everything in the selected repositories.
Before the second run, edit a comment, rename a milestone or delete an issue on
GitHub: only the affected issues are fetched again, unchanged listings answer
``304`` and cost no rate-limit quota, and the deleted issue is forgotten.

Privacy / opt-in
----------------
This reads the content of your issues and pull requests. Nothing is fetched
until you run this script. Scope what you ingest with ``GITHUB_REPOS``, and use
a dedicated dataset so you can wipe it with a single ``cognee.forget``.

One-time setup
--------------
1. Install the package:

       uv pip install cognee-community-connector-github

2. Create a token at https://github.com/settings/tokens (classic or
   fine-grained; see the README for the permissions).
3. Export the token, the repositories to ingest and your LLM key, then run:

       export GITHUB_TOKEN="..."
       export GITHUB_REPOS="owner/name,owner/other"
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

   Set ``GITHUB_INCLUDE_COMMITS=1`` to also ingest default-branch commit messages.
"""

import asyncio
import os

import cognee

from cognee_community_connector_github import github_source

# Keep GitHub in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "github"


async def main() -> None:
    repos = [repo.strip() for repo in os.environ.get("GITHUB_REPOS", "").split(",") if repo.strip()]
    if not os.environ.get("GITHUB_TOKEN") or not repos:
        print("Set GITHUB_TOKEN and GITHUB_REPOS (comma-separated owner/name) to run this.")
        return

    include_commits = os.environ.get("GITHUB_INCLUDE_COMMITS") == "1"
    print(f"Syncing {len(repos)} GitHub repo(s) into cognee ...")
    source = github_source(repos=repos, include_commits=include_commits)
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        # Required: the default "replace" would drop every issue that is not
        # re-emitted on an incremental run.
        write_disposition="merge",
        # 0 = no row cap, so orphan cleanup compares against the whole corpus.
        max_rows_per_table=0,
    )
    # scanned, skipped, failed, deleted, kept, given_up (+ failed_<reason> on an early stop)
    print("Sync stats:", source.cognee_sync_stats)

    answer = await cognee.search(
        query_text="What is being worked on in these repositories, and what is still open?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit a comment, rename a milestone or delete an issue on GitHub, then re-run: "
        "only the changes are synced and deleted issues are removed from memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
