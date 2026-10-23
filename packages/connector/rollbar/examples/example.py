"""Rollbar connector demo — "what keeps breaking, and what did we already fix?".

Pull a Rollbar project's error items into cognee memory, incrementally, with
forget-on-delete: one document per item with its resolution state and a short
stack-trace excerpt, never one per occurrence.

This example is built on cognee's DLT ingestion subsystem: ``rollbar_source``
returns a ``dlt`` resource that you hand straight to ``cognee.remember``. The first
run backfills the project; re-running ``remember`` re-processes only items whose
document changed (resolved, muted, regressed, ...), and items whose occurrences you
delete in Rollbar are forgotten from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the package:

       pip install cognee-community-connector-rollbar

2. In Rollbar open your project → Settings → Project Access Tokens and create a
   token with the ``read`` scope only.

3. Export the token, the numeric project id (Settings → General) and, for links
   back to Rollbar, your account name as it appears in Rollbar URLs:

       export ROLLBAR_ACCESS_TOKEN="<read token>"
       export ROLLBAR_PROJECT_ID="123456"
       export ROLLBAR_ACCOUNT_SLUG="my-team"

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

Run it:

    python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_rollbar import rollbar_source

# keep the errors in their own dataset so they are easy to inspect and forget
DATASET_NAME = "rollbar_errors"

ROLLBAR_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    # lets orphan-cleanup compare against the whole synced corpus
    "max_rows_per_table": 0,
    # repeated syncs should not trigger a whole-graph enrichment pass each time
    "self_improvement": False,
}


async def sync(project_id: str) -> None:
    # the token comes from ROLLBAR_ACCESS_TOKEN
    source = rollbar_source(project_id, account_slug=os.environ.get("ROLLBAR_ACCOUNT_SLUG"))
    result = await cognee.remember(source, dataset_name=DATASET_NAME, **ROLLBAR_REMEMBER_KWARGS)
    print(result)
    print("sync stats:", source.cognee_sync_stats)


async def main():
    project_id = os.environ.get("ROLLBAR_PROJECT_ID")
    if not project_id:
        print(
            "Set ROLLBAR_PROJECT_ID (and ROLLBAR_ACCESS_TOKEN).\n"
            "See the setup steps in this file's docstring, then re-run."
        )
        return

    print("\n=== Rollbar sync #1 (backfill) ===")
    await sync(project_id)

    answer = await cognee.search(
        query_text=(
            "Which errors are still active in production, and which were fixed in what version?"
        ),
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Errors answer:", answer)

    # the same dataset reuses the persisted state: only changed items are processed,
    # and items whose occurrences were deleted in rollbar are removed from memory
    print("\n=== Rollbar sync #2 (incremental) ===")
    await sync(project_id)


if __name__ == "__main__":
    asyncio.run(main())
