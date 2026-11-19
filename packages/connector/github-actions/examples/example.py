"""GitHub Actions connector demo — turn your CI/CD history into memory.

Syncs GitHub Actions workflow definitions, run attempts, and job results from
one or more repositories into cognee. Once ingested, an AI agent can answer
questions like:

    "Which workflows are failing most often?"
    "What broke in the last deploy of owner/repo?"
    "Which jobs failed on the main branch this week?"
    "What does the CI workflow do for the payments service?"

How it works
────────────
* Workflow definitions — the connector reads .github/workflows/*.yml and
  renders each one as a structured document (triggers, job names, step names,
  action references). Raw shell scripts and env/secret values are excluded.

* Run attempts — every completed run within ``lookback_days`` is captured as a
  document. Re-runs (attempt 2, 3 …) are stored separately so flaky history is
  preserved and queryable.

* Jobs — each job gets its own document: step names, conclusions, annotations
  (requires Checks: Read permission), and a tail of the failing step's log.

Forget-on-delete
────────────────
Runs that are explicitly deleted on GitHub or cross the lookback boundary are
tombstoned automatically. Workflow files removed from .github/workflows/ are
also tombstoned. Cognee's orphan_cleanup then removes them from the graph and
vector stores.

Incremental sync
────────────────
Re-running this script only fetches what changed. Workflow files are cached by
blob SHA (unchanged files are never re-downloaded). Completed run attempts are
stored once and never re-fetched. Only new completions, re-runs, and deletions
are processed.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       pip install cognee-community-connector-github-actions

2. Create a fine-grained GitHub personal access token (PAT):
   https://github.com/settings/tokens?type=beta

   Required repository permissions:
     • Actions:  Read-only
     • Contents: Read-only   (for .github/workflows/)
     • Checks:   Read-only   (only if you want annotations)

3. Export your token and LLM key:

       export GITHUB_TOKEN="github_pat_..."
       export LLM_API_KEY="sk-..."

4. Edit REPOSITORIES below, then run:

       uv run python examples/example.py

Re-run after merging a PR or a workflow run completes to see incremental sync
and forget-on-delete in action.
"""

import asyncio
import os

import cognee

from cognee_community_connector_github_actions import github_actions_source

# ── Configure these ──────────────────────────────────────────────────────────

# One or more "owner/repo" strings you want to ingest.
REPOSITORIES = [
    "topoteretes/cognee",  # ← replace with your own repos
]

# How many days of run history to sync and retain in memory.
LOOKBACK_DAYS = 30

# Set to False if your token doesn't have Checks: Read.
INCLUDE_ANNOTATIONS = True

# Keep Cognee in its own dataset so it's easy to wipe with cognee.prune().
DATASET_NAME = "github_actions"

# ─────────────────────────────────────────────────────────────────────────────


async def main() -> None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "Set GITHUB_TOKEN to a fine-grained PAT with Actions + Contents "
            "(+ Checks if INCLUDE_ANNOTATIONS=True) read permissions."
        )
        return

    source = github_actions_source(
        repositories=REPOSITORIES,
        token=token,
        lookback_days=LOOKBACK_DAYS,
        include_annotations=INCLUDE_ANNOTATIONS,
        include_log_tail=True,
        max_log_lines=100,
    )

    print(f"Syncing GitHub Actions data for: {', '.join(REPOSITORIES)} ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)
    print("Sync complete.\n")

    # ── Example queries ──────────────────────────────────────────────────────

    queries = [
        "Which workflows are configured in the repository?",
        "What were the most recent failed runs and which jobs failed?",
        "What does the CI workflow do — what steps and actions does it use?",
    ]

    for query in queries:
        print(f"Q: {query}")
        answer = await cognee.search(
            query_text=query,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=[DATASET_NAME],
        )
        print(f"A: {answer}\n")

    print(
        "Tip: delete a workflow run on GitHub, then re-run this script.\n"
        "The connector will tombstone it and cognee will forget it."
    )


if __name__ == "__main__":
    asyncio.run(main())
