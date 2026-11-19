"""Mailchimp connector demo: sync a Mailchimp account into cognee, then ask it questions.

Each sent campaign (title, subject, audience and the text of the email) becomes
one cognee document that goes through the full cognify pipeline. Re-running only
processes what changed; a campaign deleted in Mailchimp is forgotten on the next
sync.

Privacy / opt-in
----------------
This reads your campaigns. Nothing is fetched until you run this script. Drafts
and contact notes are only read when you switch them on below, contact details
such as addresses are never read, and keep the data in its own dataset so
``cognee.forget(dataset="mailchimp")`` removes all of it.

Setup
-----
1. Create an API key in Mailchimp: profile icon, Account & billing, Extras,
   API keys, Create A Key. It ends with your data centre, for example -us22.
2. Export your settings, then run:

       export MAILCHIMP_API_KEY="...-us22"
       export MAILCHIMP_AUDIENCE_IDS="aaaaaaaaaa"   # optional
       export LLM_API_KEY="sk-..."                  # optional, see below
       uv run python examples/example.py

Without LLM_API_KEY, cognee runs in its keyless mode (local GLiNER extraction
and fastembed embeddings, downloaded on first use) and recall returns the
matching passages instead of a written answer.

Re-run after sending, renaming or deleting a campaign to see the incremental
sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_mailchimp import mailchimp_source

DATASET_NAME = "mailchimp"


async def main() -> None:
    api_key = os.environ.get("MAILCHIMP_API_KEY")
    if not api_key:
        print("Set MAILCHIMP_API_KEY to run this example.")
        return
    audiences = [a.strip() for a in os.environ.get("MAILCHIMP_AUDIENCE_IDS", "").split(",")]

    print("Syncing Mailchimp into cognee ...")
    source = mailchimp_source(
        api_key,
        audience_ids=[a for a in audiences if a] or None,
        include_unsent=False,  # True also syncs drafts and scheduled campaigns
        include_notes=False,  # True also syncs the notes kept on contacts
    )
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        # remember() defaults to "replace"; merge is what makes incremental sync
        # and the _deleted tombstones work.
        write_disposition="merge",
    )
    print(f"Sync stats: {source.cognee_sync_stats}")
    if not source.cognee_sync_stats["scanned"]:
        # recall() raises on a dataset that has never held anything.
        print(
            "Nothing to sync yet: this account has no sent campaigns in the selection. "
            "Send one, or set include_unsent=True above to also sync drafts."
        )
        return

    for question in (
        "What did our last campaign announce?",
        "When does the new checkout ship?",
    ):
        print(f"\nQ: {question}")
        for item in await cognee.recall(question, datasets=[DATASET_NAME]):
            text = item.get("text") if isinstance(item, dict) else getattr(item, "text", item)
            print(f"A: {text}")


if __name__ == "__main__":
    asyncio.run(main())
