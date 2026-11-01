"""Deel connector demo — contracts and worker-directory metadata as memory.

``deel_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``.
Contracts and people are ingested as small Markdown documents (metadata only) and go
through the normal cognify pipeline.

Run it twice:

* First run — every contract and person is fetched, cognified and searchable.
* Later runs — only records whose content changed are re-sent; records deleted in Deel
  are removed from memory on the next sync (see the README for the safety rules).

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
Contracts and worker data are sensitive HR data. By default only an allowlist of
non-identifying fields is read (role, status, team, dates, ids). Worker names/emails
need ``include_pii=True``; contract documents need ``include_contract_documents=True``.
Use a dedicated dataset so you can wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. In Deel, open Developer Center and create an *organization* API token with the
   ``contracts:read`` and ``people:read`` scopes. Sandbox and production tokens differ.
2. Export the token and base URL (never paste the token into code), plus your LLM key:

       export DEEL_API_TOKEN="..."
       export DEEL_BASE_URL="https://api-staging.letsdeel.com/rest"   # sandbox
       # production: https://api.letsdeel.com/rest (the default when unset)
       export LLM_API_KEY="sk-..."

3. Run:

       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_deel import deel_source

DATASET_NAME = "deel_demo"


def build_source():
    # Token and base URL come from DEEL_API_TOKEN / DEEL_BASE_URL.
    return deel_source(
        resources=["contracts", "people"],
        # include_pii=True,                  # opt in to worker names / emails / addresses
        # include_contract_documents=True,   # opt in to contract documents (see README)
        # drop_statuses=["cancelled"],       # remove these contracts from memory entirely
    )


async def sync(label: str):
    print(f"=== {label} ===")
    result = await cognee.remember(
        build_source(),
        dataset_name=DATASET_NAME,
        primary_key="id",
        # "merge" is required: it makes re-runs incremental and lets deletions propagate
        # through orphan cleanup. With cognee's default ("replace") the connector falls
        # back to a full snapshot every run — safe, but slower and it re-sends everything.
        write_disposition="merge",
        # The dlt ingestion default caps a table at 50 rows; lift it so cleanup sees
        # the whole corpus.
        max_rows_per_table=0,
    )
    print(result)


async def main() -> None:
    if not os.environ.get("DEEL_API_TOKEN"):
        print("Set DEEL_API_TOKEN (and DEEL_BASE_URL for the sandbox) to run this example.")
        return

    # First run: full ingest. Later runs only process what changed or was deleted.
    await sync("Sync (first run ingests everything; re-runs are incremental)")

    answer = await cognee.recall("Which teams have in-progress contracts, and for which roles?")
    print("\nRecall:", answer)

    # Run the script again (or call sync() again) after changing or deleting a contract
    # in Deel to see the incremental update and forget-on-delete in action.


if __name__ == "__main__":
    asyncio.run(main())
