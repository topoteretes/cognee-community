"""Snyk connector demo — turn your Snyk issues into memory.

Pull Snyk issues into cognee, with forget-on-delete. ``snyk_source`` returns
a ``dlt`` source you hand straight to ``cognee.remember`` — no routing kwargs
needed. Issues are ingested as normal documents (so they go through the full
cognify entity-extraction pipeline, unlike the relational dlt connectors).

Each run is a full snapshot: unchanged issues keep a stable id and are not
re-cognified, and issues you fix or delete in Snyk drop out of the snapshot,
so cognee's orphan cleanup forgets them from memory on the next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads your Snyk issue data. It is strictly opt-in — nothing is fetched
until you run this script. Use a dedicated dataset so you can wipe it with a
single ``cognee.prune``. Revoke the token when you are done testing.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the extra:

       pip install "cognee[snyk]"      # or: uv sync --extra snyk

2. Copy your API token from Snyk account settings and find your organization
   ID in organization settings. Tokens are region-specific — if your account
   lives in another region, pass base_url= accordingly.
3. Export the token, org, and your LLM key, then run:

       export SNYK_TOKEN="..."
       export SNYK_ORG_ID="..."
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

Re-run after fixing an issue to see the re-sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_snyk import snyk_source

# Keep Snyk in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "snyk"


async def main() -> None:
    if not os.environ.get("SNYK_TOKEN") or not os.environ.get("SNYK_ORG_ID"):
        print("Set SNYK_TOKEN and SNYK_ORG_ID to run this example.")
        return

    source = snyk_source()

    print("Syncing Snyk issues into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME)

    answer = await cognee.search(
        query_text="What are the most severe open vulnerabilities and how do we fix them?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nFix an issue in Snyk, then re-run: edits re-sync and "
        "fixed/removed issues are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
