"""Sync a GCS prefix into Cognee and search it.

From this package directory, run:
    uv run python examples/example.py --help

Set GCS_BUCKET, GCS_PREFIX, and your Cognee model credentials before syncing.
Google authentication uses Application Default Credentials, or an explicit
service-account JSON path passed with --credentials-path.
"""

import argparse
import asyncio
import os


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default=os.getenv("GCS_BUCKET"), help="GCS bucket name")
    parser.add_argument("--prefix", default=os.getenv("GCS_PREFIX"), help="Object name prefix")
    parser.add_argument("--credentials-path", help="Optional service-account JSON key path")
    parser.add_argument("--dataset", default="gcs_demo", help="Dedicated Cognee dataset")
    parser.add_argument("--question", default="Summarize the documents in this dataset.")
    args = parser.parse_args()
    if not args.bucket or not args.bucket.strip():
        parser.error("set GCS_BUCKET or pass --bucket")
    if args.bucket.startswith("gs://"):
        parser.error("--bucket must be a bucket name without gs:// or an object path")
    if not args.prefix or not args.prefix.strip():
        parser.error("set GCS_PREFIX or pass a non-empty --prefix, such as docs/")
    return args


async def main(args: argparse.Namespace) -> None:
    import cognee
    from cognee.modules.retrieval.exceptions.exceptions import NoDataError
    from cognee_community_connector_gcs import gcs_source

    source = gcs_source(
        bucket_name=args.bucket,
        prefix=args.prefix,
        credentials_path=args.credentials_path,
    )
    print("Syncing the selected GCS prefix into Cognee ...")
    result = await cognee.remember(
        source,
        dataset_name=args.dataset,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        run_in_background=False,
        self_improvement=False,
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
    print("Edit or delete a test object and run the same command again to sync changes.")


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
