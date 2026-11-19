"""Git history connector demo — turn a repo's commit history into memory.

Walks a local Git repository's commit history and ingests each commit as a
document (subject, body, author, date, changed files, trailers). ``why`` a
change was made usually lives in the commit message, not the diff, so this is
treated as prose and routed through cognee's normal entity-extraction path —
like the Notion connector, not like a relational dlt source.

Sync is incremental and keyed on commit SHA (a SHA never changes once made):
only new commits are re-ingested each run, and commits that are no longer
reachable from the configured refs (rebase, force-push, history rewrite,
branch deletion) are forgotten on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads commit history from a local clone you point it at. Nothing leaves
your machine except what cognee sends to your configured LLM/vector/graph
backends. No network calls are made by this connector itself.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[githistory]"   # or: uv sync --extra githistory

2. Have a local Git clone to point at (any repo on disk; a shallow clone is
   rejected — run `git fetch --unshallow` first).
3. Export your LLM key, then run:

       export LLM_API_KEY="sk-..."
       uv run python examples/example.py /path/to/some/repo

Re-run after new commits land, or after a rebase/force-push, to see the
incremental sync and forget-on-delete.
"""

import asyncio
import sys

import cognee

from cognee_community_connector_githistory import githistory_source

DATASET_NAME = "githistory"


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python example.py /path/to/local/git/repo")
        return
    repo_path = sys.argv[1]

    # Walk HEAD by default; pass refs=["main", "release/1.0"] for more, or
    # paths=["src/"] to scope to a subdirectory's history.
    source = githistory_source(repo_path)

    print(f"Syncing commit history from {repo_path} ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What were the most significant changes and why were they made?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nMake a new commit, or rebase/force-push to rewrite history, then "
        "re-run: new commits are added, and commits no longer reachable from "
        "the configured refs are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
