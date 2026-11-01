"""Sync selected Jenkins jobs into cognee and search their build history."""

import asyncio
import os

import cognee

from cognee_community_connector_jenkins import jenkins_source


async def main() -> None:
    required = ("JENKINS_URL", "JENKINS_USER", "JENKINS_API_TOKEN")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print(f"Set {', '.join(missing)} to connect to Jenkins.")
        return

    selected_jobs = os.environ.get("JENKINS_JOB_NAMES") or ""
    job_names = [name.strip() for name in selected_jobs.split(",") if name.strip()] or None

    await cognee.remember(
        jenkins_source(job_names=job_names),
        dataset_name="jenkins",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    answer = await cognee.search(
        query_text="Which Jenkins builds failed and what did their logs report?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["jenkins"],
    )
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
