"""Ingest a SharePoint document library into cognee memory, incrementally.

Demonstrates the SharePoint / OneDrive DLT connector: PDF, Word, Excel and
plain-text files in a site's document libraries are extracted, chunked, and
cognified like any other document. Re-running this script only re-processes
files that changed since the last run, and files deleted upstream are forgotten
automatically.

Install:
    pip install "cognee[sharepoint]"

Auth setup — Microsoft Graph app-only, so there is no interactive step and this
can run on a schedule. The README has the click-by-click version; in short:

  - Register an app in Entra ID (portal.azure.com -> Microsoft Entra ID ->
    App registrations -> New registration).
  - Add the Microsoft Graph *application* permission Files.Read.All, then have a
    tenant administrator click "Grant admin consent".
  - Create a client secret and copy its Value immediately.
  - Find the site id with:
        GET https://graph.microsoft.com/v1.0/sites/{hostname}:/sites/{sitePath}
  - Set:
        MICROSOFT_TENANT_ID=<Directory (tenant) ID>
        MICROSOFT_CLIENT_ID=<Application (client) ID>
        MICROSOFT_CLIENT_SECRET=<the secret Value>
        SHAREPOINT_SITE_ID=<hostname,site-guid,web-guid>
        # optional: SHAREPOINT_DRIVE_IDS=<comma-separated library ids>

Also set the usual cognee LLM_API_KEY (see .env.template) — this example calls
cognee.recall(), which needs an LLM for the final completion.
"""

import asyncio
import os

import cognee

from cognee_community_connector_sharepoint import sharepoint_source

DATASET_NAME = "sharepoint_demo"

CREDENTIAL_ENV = ("MICROSOFT_TENANT_ID", "MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET")


async def main() -> None:
    has_scope = os.environ.get("SHAREPOINT_SITE_ID") or os.environ.get("SHAREPOINT_DRIVE_IDS")
    if not has_scope or any(not os.environ.get(name) for name in CREDENTIAL_ENV):
        print(
            "Set "
            + ", ".join(CREDENTIAL_ENV)
            + " and SHAREPOINT_SITE_ID (or SHAREPOINT_DRIVE_IDS) to run this example."
        )
        return

    print("=== Initial sync ===")
    result = await cognee.remember(
        sharepoint_source(),  # reads the MICROSOFT_* / SHAREPOINT_* env vars
        dataset_name=DATASET_NAME,
        primary_key="id",
        # "merge" is required: it is what makes re-runs incremental and what
        # makes deletions propagate via orphan cleanup.
        write_disposition="merge",
    )
    print(result)

    answer = await cognee.recall("Summarize what's in the SharePoint library.")
    print("Recall:", answer)

    print("\n=== Incremental re-sync (only changed/removed files are processed) ===")
    result = await cognee.remember(
        sharepoint_source(),
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
    )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
