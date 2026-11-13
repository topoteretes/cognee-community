"""Bitbucket connector demo — turn your pull requests into memory.

Pull Bitbucket Cloud pull requests and PR comments into cognee, incrementally,
with forget-on-delete. ``bitbucket_source`` returns a ``dlt`` source you hand
to ``cognee.remember``. Pull requests and comments are ingested as normal
documents (so they go through the full cognify entity-extraction pipeline,
unlike the relational dlt connectors).

``write_disposition="merge"`` below is REQUIRED, not just a default: it's
what makes a re-run incremental (only changed pull requests are re-fetched)
and what makes deletions actually propagate (cognee's add pipeline defaults
to "replace", which would re-fetch and re-cognify everything from scratch on
every run instead). ``max_rows_per_table=0`` lifts cognee's default 50-row
read-back cap so forget-on-delete compares against the whole synced corpus,
not a truncated window.

Re-running this script resumes from the cursor persisted by the previous
run (in dlt's pipeline state) and only re-fetches what changed: pull requests
updated since last time, plus comments on every pull request still open.
Longer-settled changes -- a comment deleted on a pull request that's been
closed for a while, or a pull request dropped by narrowing ``pr_states`` --
are caught on the periodic full reconciliation pass (every 10 runs by
default; see ``full_sync_every`` and the README's "How sync + forget-on-delete
work" section).

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of your Bitbucket pull requests and comments. It is
strictly opt-in — nothing is fetched until you run this script. Scope what
you ingest with ``repo_slugs``, and use a dedicated dataset so you can wipe it
with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Note your Bitbucket Cloud workspace id (the slug in
   ``bitbucket.org/<workspace>/...`` URLs).
2. Create a personal API token: Account settings -> Security ->
   "Create API token with scopes" -> app "Bitbucket" -> scopes
   read:workspace:bitbucket, read:repository:bitbucket, read:pullrequest:bitbucket.
3. Export the token and your LLM key, then run:

       export BITBUCKET_WORKSPACE="my-team"
       export BITBUCKET_EMAIL="you@example.com"
       export BITBUCKET_API_TOKEN="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

   Optionally set ``BITBUCKET_REPOS`` to a comma-separated list of repo slugs
   to restrict ingestion; omit it to sync every repo in the workspace.

Re-run this script after editing a pull request or deleting a comment in
Bitbucket: the first run backfills everything (a full pass); every run after
that is incremental and only re-fetches what changed, picking up the edit or
reconciling the deletion out of memory.
"""

import asyncio
import os

import cognee

from cognee_community_connector_bitbucket import bitbucket_source

# Keep Bitbucket in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "bitbucket"

# Routing kwargs shared by every remember() call below. write_disposition and
# max_rows_per_table are explained in the module docstring; both are required
# here, not optional tuning.
BITBUCKET_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def main() -> None:
    workspace = os.environ.get("BITBUCKET_WORKSPACE")
    if not workspace or not os.environ.get("BITBUCKET_API_TOKEN"):
        print(
            "Set BITBUCKET_WORKSPACE and BITBUCKET_API_TOKEN (plus BITBUCKET_EMAIL for "
            "Basic auth) to run this example."
        )
        return

    repos = os.environ.get("BITBUCKET_REPOS")
    repo_slugs = [slug.strip() for slug in repos.split(",") if slug.strip()] if repos else None

    source = bitbucket_source(workspace=workspace, repo_slugs=repo_slugs)

    print("Syncing Bitbucket pull requests into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME, **BITBUCKET_REMEMBER_KWARGS)

    answer = await cognee.search(
        query_text="Summarize what these pull requests are about.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit a pull request or delete a comment in Bitbucket, then re-run this script: "
        "the next sync is incremental (only the change is re-fetched) and reconciles the "
        "edit/deletion out of memory without re-processing everything else."
    )


if __name__ == "__main__":
    asyncio.run(main())
