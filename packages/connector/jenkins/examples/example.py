"""Jenkins connector demo — "ask my CI".

Pull Jenkins jobs and builds into cognee memory, incrementally, with
forget-on-delete, then ask why builds failed.

``jenkins_source`` returns a ``dlt`` resource that you hand straight to
``cognee.remember``. The first run ingests each job and its most recent builds
(with the tail of every failed build's console log); re-running ``remember``
fetches only builds that finished since, and jobs or builds deleted in Jenkins
are forgotten on the next sync.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Install the connector (from this folder):

       uv sync

2. In Jenkins, create an API token: your user (top-right) → Security →
   API Token → Add new Token. Read access to the jobs is enough — the
   connector only issues GET requests.

3. Export your connection details:

       export JENKINS_URL="https://jenkins.example.com"
       export JENKINS_USER="you"
       export JENKINS_API_TOKEN="…"
       # optional: export JENKINS_JOBS="backend/main,nightly-e2e"

4. Set your LLM key (``LLM_API_KEY``) in ``.env`` like any other cognee example.

No Jenkins at hand? ``docker run --rm -p 8080:8080 jenkins/jenkins:lts-jdk21``
gives you a local one to try against.

Run it:

    uv run python examples/example.py
"""

import asyncio
import os

import cognee

from cognee_community_connector_jenkins import jenkins_source

# Keep CI history in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "jenkins_ci"

# ``write_disposition="merge"`` makes re-syncs incremental (the add pipeline
# defaults to "replace"); ``max_rows_per_table=0`` lifts cognee's 50-row read
# cap so forget-on-delete compares against the whole synced history.
JENKINS_REMEMBER_KWARGS = {
    "primary_key": "id",
    "write_disposition": "merge",
    "max_rows_per_table": 0,
}


async def main():
    base_url = os.environ.get("JENKINS_URL")
    username = os.environ.get("JENKINS_USER")
    api_token = os.environ.get("JENKINS_API_TOKEN")
    jobs = os.environ.get("JENKINS_JOBS")

    if not all([base_url, username, api_token]):
        print(
            "Set JENKINS_URL, JENKINS_USER and JENKINS_API_TOKEN.\n"
            "See the setup steps in this file's docstring, then re-run."
        )
        return

    # Start from a clean slate so the demo is reproducible.
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    def build_source():
        return jenkins_source(
            base_url=base_url,
            username=username,
            api_token=api_token,
            job_names=[j.strip() for j in jobs.split(",")] if jobs else None,
            log_results=("FAILURE", "UNSTABLE"),
        )

    # ── First sync: jobs + recent builds ───────────────────────────────────
    print("\n=== Jenkins sync #1 ===")
    print(await cognee.remember(build_source(), dataset_name=DATASET_NAME, **JENKINS_REMEMBER_KWARGS))

    answer = await cognee.search(
        query_text="Which builds failed, and what does the console log say caused it?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("Failures:", answer)

    # ── Second sync: only new builds; deleted jobs/builds are forgotten ────
    print("\n=== Jenkins sync #2 (incremental) ===")
    print(await cognee.remember(build_source(), dataset_name=DATASET_NAME, **JENKINS_REMEMBER_KWARGS))


if __name__ == "__main__":
    asyncio.run(main())
