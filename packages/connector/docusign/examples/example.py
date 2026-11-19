"""DocuSign connector demo — turn your signed agreements into memory.

Pull completed DocuSign envelopes into cognee, incrementally and with
forget-on-delete. ``docusign_source`` returns a ``dlt`` source you hand
straight to ``cognee.remember``. Each envelope is ingested as one document: a
short header (subject, sender, each signer with role and signing date, custom
fields, what signers typed into form fields) followed by the text of the signed
PDF. It goes through the full cognify entity-extraction pipeline, so signers
and companies become graph nodes linked to the agreement.

The first run lists every envelope in scope. Later runs ask DocuSign only for
envelopes whose status changed since the previous run. Envelopes that are
voided, moved to the Deleted folder or purged in DocuSign are forgotten on the
next sync.

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the contents of your signed agreements and stores their text in
your cognee databases (and sends it to your LLM provider during cognify). It
is strictly opt-in: nothing is fetched until you run this script. Use a
dedicated dataset so you can wipe it with a single ``cognee.forget``, and a
DocuSign API user that can only see the envelopes you want to ingest.

────────────────────────────────────────────────────────────────────────────
One-time setup (see README.md for the click-by-click version)
────────────────────────────────────────────────────────────────────────────
1. Create a free developer account at https://developers.docusign.com and add
   an integration key (RSA keypair) in Apps and Keys.
2. Grant consent once for the user the integration will act as.
3. Export the credentials and your LLM key, then run:

       export DOCUSIGN_INTEGRATION_KEY="<integration key>"
       export DOCUSIGN_USER_ID="<API user GUID>"
       export DOCUSIGN_PRIVATE_KEY_PATH="private.pem"
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

   For a quick test, ``DOCUSIGN_ACCESS_TOKEN`` (an hour-long token from the
   developer console) replaces the first three.

Re-run after completing, deleting or purging an envelope to see the
incremental re-sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_docusign import docusign_source

# Keep DocuSign in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "docusign"


async def main() -> None:
    has_token = bool(os.environ.get("DOCUSIGN_ACCESS_TOKEN"))
    jwt_variables = (
        "DOCUSIGN_INTEGRATION_KEY",
        "DOCUSIGN_USER_ID",
        "DOCUSIGN_PRIVATE_KEY",
        "DOCUSIGN_PRIVATE_KEY_PATH",
    )
    has_jwt = all(os.environ.get(name) for name in jwt_variables[:2]) and any(
        os.environ.get(name) for name in jwt_variables[2:]
    )
    if not (has_token or has_jwt):
        print(
            "Set DOCUSIGN_ACCESS_TOKEN, or DOCUSIGN_INTEGRATION_KEY + DOCUSIGN_USER_ID + "
            "DOCUSIGN_PRIVATE_KEY_PATH (see README.md) to run this example."
        )
        return

    # Scope what is ingested. Completed envelopes only by default; add
    # folder_ids=[...] to limit it to a folder, and node_set_field="agreement_type"
    # to group envelopes by a custom field so recall can be scoped to, say, NDAs.
    source = docusign_source(
        environment=os.environ.get("DOCUSIGN_ENVIRONMENT", "demo"),
        statuses=["completed"],
    )

    print("Syncing DocuSign envelopes into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        # Required: each run after the first only fetches what changed.
        write_disposition="merge",
    )
    print("Sync stats:", getattr(source, "cognee_sync_stats", {}))

    answer = await cognee.search(
        query_text="Who signed which agreements, and when?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nComplete, void, delete or purge an envelope in DocuSign, then re-run: new "
        "agreements are picked up and removed ones are forgotten."
    )


if __name__ == "__main__":
    asyncio.run(main())
