"""Sync an Azure Blob container prefix into Cognee and search its documents.

Run from this package directory: uv run python examples/example.py --help
Set AZURE_STORAGE_ACCOUNT_URL and AZURE_STORAGE_SAS_TOKEN or AZURE_STORAGE_KEY,
along with your Cognee model credentials, before syncing.
"""

import argparse
import asyncio
import os


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default=os.getenv("AZURE_STORAGE_CONTAINER"))
    parser.add_argument("--prefix", default=os.getenv("AZURE_STORAGE_PREFIX"))
    parser.add_argument("--account-url", help="Account endpoint; otherwise use the environment")
    parser.add_argument("--dataset", default="azure_blob_demo", help="Dedicated Cognee dataset")
    parser.add_argument("--max-objects", type=int, default=1000)
    parser.add_argument("--max-object-size", type=int, default=10 * 1024 * 1024, help="Bytes")
    parser.add_argument("--question", default="Summarize the documents in this dataset.")
    args = parser.parse_args()
    if not args.container or not args.container.strip():
        parser.error("set AZURE_STORAGE_CONTAINER or pass --container")
    if not args.prefix or not args.prefix.strip():
        parser.error("set AZURE_STORAGE_PREFIX or pass a non-empty --prefix, such as docs/")
    if args.max_objects < 1 or args.max_object_size < 1:
        parser.error("--max-objects and --max-object-size must be positive")
    return args


async def main(args: argparse.Namespace) -> None:
    import cognee
    from cognee.modules.retrieval.exceptions.exceptions import NoDataError
    from cognee_community_connector_azure_blob import azure_blob_source

    source = azure_blob_source(
        container_name=args.container,
        prefix=args.prefix,
        account_url=args.account_url,
        max_objects=args.max_objects,
        max_object_size=args.max_object_size,
    )
    print("Syncing the selected Azure Blob prefix into Cognee ...")
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
    print("Edit or delete a test blob and run the same command again to sync changes.")


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
