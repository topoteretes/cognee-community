"""GitHub Actions connector demo — turn your CI history into memory.

Pull completed workflow runs into cognee, with forget-on-delete.
``github_actions_source`` returns a ``dlt`` source you hand straight to
``cognee.remember`` — no routing kwargs needed. Runs are ingested as normal
documents, so they go through the full cognify entity-extraction pipeline.

Each sync is a full snapshot of the most recent ``max_runs_per_repo`` completed
runs per repository: unchanged runs keep a stable id and are not re-cognified,
re-runs are re-synced, and runs that fall out of the window (or out of GitHub's
retention period) are forgotten by cognee's orphan cleanup on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads run metadata (commit messages, branch names, actors) and, if you set
``include_failed_logs=True``, the errors and output of failed job logs — which contain
whatever your workflows print. Nothing is fetched until you run this script.
Use a dedicated dataset so you can wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Create a token: a fine-grained token with "Actions: read" on the target
   repositories, or a classic token (``repo`` scope for private repositories).
2. Export it, the repositories, and your LLM key, then run:

       export GITHUB_TOKEN="github_pat_..."
       export GITHUB_REPOS="owner/repo,owner/other-repo"
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after new workflow runs complete to see the re-sync.
"""

import asyncio
import os

import cognee

from cognee_community_connector_github_actions import github_actions_source

# Keep GitHub Actions in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "github-actions"


async def main() -> None:
    repos = [r.strip() for r in os.environ.get("GITHUB_REPOS", "").split(",") if r.strip()]
    if not os.environ.get("GITHUB_TOKEN") or not repos:
        print(
            "Set GITHUB_TOKEN and GITHUB_REPOS (owner/repo, comma-separated) to run this example."
        )
        return

    # Narrow with workflow="ci.yml", branch="main", or created_after="2026-01-01".
    source = github_actions_source(repos=repos, max_runs_per_repo=50, include_failed_logs=True)

    print(f"Syncing GitHub Actions runs for {', '.join(repos)} into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="Which workflows failed recently, and why?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
