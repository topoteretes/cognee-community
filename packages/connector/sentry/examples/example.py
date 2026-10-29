"""Sentry connector demo - ask questions about what has been breaking.

Each grouped issue becomes one document (never one per raw event) with its title,
status, counts, culprit, assignee and a bounded sample of in-app stack frames.

Each run re-lists every issue, because resolving an issue moves no timestamp (checked
against a live org), and Sentry's issue-update handler writes no timestamp for archiving
or assigning either; only documents whose text changed are re-ingested,
and issues Sentry has deleted or merged away are forgotten.

Setup:
1. In Sentry, create an internal integration (Settings -> Developer Settings ->
   New Internal Integration) with Organization: Read, Project: Read and
   Issue & Event: Read, and copy its token.
2. Run:

       export SENTRY_AUTH_TOKEN="sntrys_..."
       export SENTRY_ORG="my-org"
       export SENTRY_HOST="sentry.io"        # optional: de.sentry.io, or self-hosted
       export SENTRY_PROJECTS="backend-api"  # optional, comma separated
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_sentry import sentry_source

DATASET_NAME = "sentry"


async def main() -> None:
    auth_token = os.environ.get("SENTRY_AUTH_TOKEN")
    org_slug = os.environ.get("SENTRY_ORG")
    if not auth_token or not org_slug:
        print("Set SENTRY_AUTH_TOKEN and SENTRY_ORG to run this example.")
        return
    projects = [
        slug.strip() for slug in os.environ.get("SENTRY_PROJECTS", "").split(",") if slug.strip()
    ]

    source = sentry_source(
        auth_token=auth_token,
        org_slug=org_slug,
        host=os.environ.get("SENTRY_HOST", "sentry.io"),
        project_slugs=projects or None,
    )
    print(f"Syncing Sentry issues of {org_slug} into cognee ...")
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        write_disposition="merge",
        self_improvement=False,  # no enrichment pass on every scheduled sync
    )
    print("Sync stats:", source.cognee_sync_stats)

    answer = await cognee.search(
        query_text="Which parts of the codebase fail most often, and what is still unresolved?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\n", answer)


if __name__ == "__main__":
    asyncio.run(main())
