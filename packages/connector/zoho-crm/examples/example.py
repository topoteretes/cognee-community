"""Zoho CRM connector demo - turn your CRM into memory.

Pull Zoho CRM records (Leads, Contacts, Accounts, Deals), their notes and,
optionally, text attachments into cognee, incrementally and with
forget-on-delete. ``zoho_crm_source`` returns a ``dlt`` source you hand to
``cognee.remember``.

First run: backfills everything. Later runs: only records changed since the last
run are fetched (``If-Modified-Since``), and records deleted in Zoho are
forgotten.

Privacy: this reads customer data. Nothing is fetched until you run it. The OAuth
scope is read-only and e-mail/phone fields are left out by default.

Setup (see README for the Self Client steps):

    cd packages/connector/zoho-crm && uv sync --all-extras
    export ZOHO_CLIENT_ID="1000...."
    export ZOHO_CLIENT_SECRET="..."
    export ZOHO_REFRESH_TOKEN="1000...."
    export ZOHO_REGION="eu"            # com, eu, in, com.au, jp, com.cn, ca, sa
    export LLM_API_KEY="sk-..."
    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_zoho_crm import zoho_crm_source

DATASET_NAME = "zoho_crm"


async def main() -> None:
    required = ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
    if not all(os.environ.get(name) for name in required):
        print("Set ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN to run this example.")
        return

    # Limit with modules=[...]; add include_attachments=True for text files.
    source = zoho_crm_source()

    print("Syncing Zoho CRM into cognee ...")
    # write_disposition="merge" is REQUIRED: incremental runs only see changes.
    await cognee.remember(source, dataset_name=DATASET_NAME, write_disposition="merge")

    answer = await cognee.search(
        query_text="Which deals are open, and what do the notes say about them?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print("\nEdit or delete a record in Zoho, then re-run: only the changes are fetched.")


if __name__ == "__main__":
    asyncio.run(main())
