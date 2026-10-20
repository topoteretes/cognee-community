"""Sync GitLab issues, merge requests, comments and wiki pages into Cognee, then search them.

Run from this package directory:
    uv run python examples/example.py --help

Set GITLAB_TOKEN (a legacy personal access token with read_api) and your Cognee model
credentials first. Run the same command again to sync later changes and deletions.
"""

import argparse
import asyncio
import os

RESOURCE_TYPES = ("issues", "merge_requests", "comments", "wiki")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("GITLAB_BASE_URL", "https://gitlab.com"),
        help="GitLab instance URL (default: GITLAB_BASE_URL or https://gitlab.com)",
    )
    parser.add_argument(
        "--projects",
        default=os.getenv("GITLAB_PROJECT_IDS"),
        help="Comma-separated project IDs or group/project paths "
        "(default: GITLAB_PROJECT_IDS, or every project you are a member of)",
    )
    parser.add_argument(
        "--resources",
        default=",".join(RESOURCE_TYPES),
        help=f"Comma-separated resource types from: {', '.join(RESOURCE_TYPES)}",
    )
    parser.add_argument("--dataset", default="gitlab", help="Dedicated Cognee dataset")
    parser.add_argument("--question", default="Summarize the open issues and their discussion.")
    args = parser.parse_args()
    if not os.getenv("GITLAB_TOKEN", "").strip():
        parser.error("set GITLAB_TOKEN to a legacy personal access token with read_api")
    args.resources = [name.strip() for name in args.resources.split(",") if name.strip()]
    if args.projects:
        refs = [ref.strip() for ref in args.projects.split(",") if ref.strip()]
        args.projects = [int(ref) if ref.isdigit() else ref for ref in refs]
    return args


async def main(args: argparse.Namespace) -> None:
    import cognee
    from cognee.modules.retrieval.exceptions.exceptions import NoDataError

    from cognee_community_connector_gitlab import gitlab_source

    source = gitlab_source(
        token=os.environ["GITLAB_TOKEN"],
        base_url=args.base_url,
        resource_types=args.resources,
        project_ids=args.projects or None,
    )
    print("Syncing GitLab into Cognee ...")
    result = await cognee.remember(
        source,
        dataset_name=args.dataset,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        run_in_background=False,
    )
    if result.status != "completed":
        raise RuntimeError(result.error or f"Sync did not complete: {result.status}")
    print("Sync completed. Searching the dataset ...")
    try:
        answer = await cognee.search(
            query_text=args.question,
            query_type=cognee.SearchType.GRAPH_COMPLETION,
            datasets=[args.dataset],
        )
    except NoDataError:
        print("No searchable documents remain in this dataset.")
    else:
        print(answer)
    print("Edit or delete an issue, MR, comment or wiki page and run again to sync the change.")


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
