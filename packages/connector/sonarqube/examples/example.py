"""SonarQube connector demo: turn your SonarQube projects into memory.

Pulls unresolved issues, TO_REVIEW Security Hotspots and quality-gate outcomes
into cognee, incrementally and with forget-on-delete. ``sonarqube_source``
returns a ``dlt`` source you hand to ``cognee.remember``. Rows are ingested as
normal documents, so they go through the full cognify entity-extraction
pipeline.

Run it twice. The first run ingests everything in the selected project(s).
Before the second run, resolve an issue or review a hotspot in SonarQube: it
drops out of the next sync and is forgotten from memory, while an unchanged
issue produces no write at all.

Privacy / opt-in
----------------
This reads issue messages and file paths, which can reveal source code
structure and content. The issue author is never included unless you pass
``include_author=True``. Nothing is fetched until you run this script. Scope
what you ingest with ``project_keys``, and use a dedicated dataset so you can
wipe it with a single ``cognee.forget``.

One-time setup
--------------
1. Install the package:

       uv pip install cognee-community-connector-sonarqube

2. Generate a SonarQube user token: My Account > Security > Generate Tokens.
3. Export the token, the SonarQube URL, the project(s) to ingest and your LLM
   key, then run:

       export SONARQUBE_TOKEN="squ_..."
       export SONARQUBE_URL="https://sonarqube.example.com"   # or https://sonarcloud.io
       export SONARQUBE_PROJECTS="my_project"
       export LLM_API_KEY="sk-..."
       uv run python examples/example.py

   On SonarQube Cloud, also export SONARQUBE_ORGANIZATION.
"""

import asyncio
import os

import cognee

from cognee_community_connector_sonarqube import sonarqube_source

# Keep SonarQube in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "sonarqube"


async def main() -> None:
    raw_projects = os.environ.get("SONARQUBE_PROJECTS", "")
    project_keys = [p.strip() for p in raw_projects.split(",") if p.strip()]
    if not os.environ.get("SONARQUBE_TOKEN") or not os.environ.get("SONARQUBE_URL"):
        print("Set SONARQUBE_TOKEN and SONARQUBE_URL to run this.")
        return
    if not project_keys:
        print("Set SONARQUBE_PROJECTS (comma-separated project keys) to run this.")
        return

    print(f"Syncing {len(project_keys)} SonarQube project(s) into cognee ...")
    source = sonarqube_source(project_keys=project_keys)
    await cognee.remember(
        source,
        dataset_name=DATASET_NAME,
        primary_key="id",
        # Required: the dlt resource declares write_disposition="merge", but
        # cognee.remember() reads this from its own kwargs, not the resource --
        # the default "replace" would drop everything not re-emitted this run.
        write_disposition="merge",
        # 0 = no row cap, so orphan cleanup compares against the whole corpus.
        max_rows_per_table=0,
    )
    # emitted, unchanged, kept, deleted, spot_checks, projects, missing_fields
    print("Sync stats:", source.cognee_sync_stats)

    answer = await cognee.search(
        query_text="What are the most severe open issues, and is the quality gate passing?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nResolve an issue or review a hotspot in SonarQube, then re-run: only the "
        "changes are synced, and anything that no longer matches the filters is "
        "removed from memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
