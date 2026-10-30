"""Outreach connector demo — sequences, prospects and activity as memory.

``outreach_source`` returns a ``dlt`` source you hand straight to ``cognee.remember``.
Records are ingested as small Markdown documents and go through the normal cognify pipeline.

Run it twice:

* First run — everything in scope is fetched, cognified and searchable.
* Later runs — only records changed since the stored ``updatedAt`` cursor are re-sent; records
  deleted in Outreach are removed from memory on the next sync (id sweep, see the README).

NOT TESTED AGAINST A LIVE OUTREACH ACCOUNT. See the README's "Testing status".

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
Prospects are personal data. By default only an allowlist of non-contact fields is read:
emails, phone numbers and addresses need ``include_contact_details=True``; email text, call
and task notes and meeting titles need ``include_activity_bodies=True``. Use a dedicated
dataset so you can wipe it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Create an Outreach OAuth app (see README), register the redirect URI
   ``http://localhost:8765/callback`` and enable the read scopes.
2. Export the app credentials (never paste them into code), plus your LLM key:

       export OUTREACH_CLIENT_ID="..."
       export OUTREACH_CLIENT_SECRET="..."
       export LLM_API_KEY="sk-..."

3. Authorize once (opens a browser, stores tokens in a 0600 file):

       uv run outreach-auth

4. Run:

       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_outreach import outreach_source

DATASET_NAME = "outreach_demo"


def build_source():
    # Credentials come from OUTREACH_CLIENT_ID / OUTREACH_CLIENT_SECRET and the token file
    # written by `outreach-auth` (override its location with OUTREACH_TOKEN_STORE).
    return outreach_source(
        resources=["sequences", "prospects", "activity"],
        activity_types=["mailings", "calls", "tasks", "meetings"],
        activity_since=90,  # days of activity on the first sync
        # include_contact_details=True,   # opt in to emails / phones / addresses
        # include_activity_bodies=True,   # opt in to email text, call/task notes, meeting text
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
    if not (os.environ.get("OUTREACH_CLIENT_ID") and os.environ.get("OUTREACH_CLIENT_SECRET")):
        print("Set OUTREACH_CLIENT_ID / OUTREACH_CLIENT_SECRET and run `outreach-auth` first.")
        return

    await sync("Sync (first run ingests everything; re-runs are incremental)")

    answer = await cognee.recall("Which prospects are in active sequences, and which sequences?")
    print("\nRecall:", answer)

    # Run the script again after changing or deleting a record in Outreach to see the
    # incremental update and forget-on-delete in action.


if __name__ == "__main__":
    asyncio.run(main())
