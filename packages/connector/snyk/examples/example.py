"""Sync Snyk vulnerability findings into cognee memory and ask a question about them.

Each vulnerability becomes one document that lists every affected package and project, its
CVE and CWE ids and the fix versions, so the same vulnerability found in many projects is
stored once. cognee chunks the documents and runs them through LLM entity extraction.

The Snyk REST API needs an Enterprise plan: tokens of Free and Team plans (and personal
tokens of trials) get HTTP 403.

Setup (see the README for details):
  1. In Snyk, create a service account (Group or Org settings > Service accounts) with a
     Group Viewer role, or an Org role that can read projects and issues, and copy its token.
  2. Find your organization id (Org settings) and your region from the app URL:
       app.snyk.io      -> us-01      app.us.snyk.io -> us-02
       app.eu.snyk.io   -> eu-01      app.au.snyk.io -> au-01
  3. Set:
       SNYK_TOKEN=<the token>
       SNYK_REGION=eu-01                    (default us-01)
       SNYK_ORG_IDS=<org id>[,<org id>]
     plus the usual cognee LLM_API_KEY.

Run:
    python examples/example.py ["optional question about your vulnerabilities"]
"""

import asyncio
import os
import sys

import cognee

from cognee_community_connector_snyk import snyk_source

DATASET = "snyk_demo"


async def main():
    missing = [name for name in ("SNYK_TOKEN", "SNYK_ORG_IDS") if not os.getenv(name)]
    if missing:
        print(f"Set {' and '.join(missing)} first. See the setup notes at the top of this file.")
        return

    source = snyk_source()  # reads SNYK_TOKEN, SNYK_REGION and SNYK_ORG_IDS

    # Run this script again to see incremental sync: only vulnerabilities that changed are
    # processed. Fix a finding in Snyk (or ignore it) and run it once more: if no finding of
    # a vulnerability is left, its document is forgotten. (Forgetting most of an organization
    # at once is double-checked against Snyk first; see allow_mass_delete.)
    print("=== Sync ===")
    await cognee.remember(
        source,
        dataset_name=DATASET,
        primary_key="id",
        # "merge" matches how the resource is declared; remember() would otherwise
        # default to "replace" and override it.
        write_disposition="merge",
        # No row cap: the default stops after 50 rows.
        max_rows_per_table=0,
    )
    # Counts only, never project names or findings.
    print("Sync stats:", source.cognee_sync_stats)

    question = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "Which critical vulnerabilities affect several projects?"
    )
    results = await cognee.search(question, datasets=[DATASET])
    print("Search:", results)


if __name__ == "__main__":
    asyncio.run(main())
