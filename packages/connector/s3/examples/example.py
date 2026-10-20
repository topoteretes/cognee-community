"""S3 connector demo — turn a bucket prefix into memory.

Pull the text objects under an S3 prefix into cognee, with incremental re-sync
and forget-on-delete. ``s3_source`` returns a ``dlt`` source you hand straight
to ``cognee.remember``. Objects are ingested as normal documents (so they go
through the full cognify entity-extraction pipeline).

Pass ``write_disposition="merge"`` so each re-run only downloads objects whose
LastModified changed, and forgets objects deleted from the bucket. Without it
the connector falls back to a full snapshot each run (still correct, but every
object is downloaded again).

────────────────────────────────────────────────────────────────────────────
Privacy / opt-in
────────────────────────────────────────────────────────────────────────────
This reads the content of objects in your bucket. Nothing is fetched until you
run this script. Only the configured prefix is read, and the sync aborts if it
holds more than ``max_objects`` objects. Use a dedicated dataset so you can wipe
it with a single ``cognee.prune``.

────────────────────────────────────────────────────────────────────────────
One-time setup
────────────────────────────────────────────────────────────────────────────
1. Create an IAM user/role with s3:ListBucket and s3:GetObject on the prefix.
2. Export its credentials, the bucket/prefix, and your LLM key, then run:

       export AWS_ACCESS_KEY_ID="<your-access-key-id>"
       export AWS_SECRET_ACCESS_KEY="<your-secret-access-key>"
       export AWS_REGION="us-east-1"
       export S3_BUCKET="<your-bucket>"
       export S3_PREFIX="docs/"
       export LLM_API_KEY="<your-llm-api-key>"
       uv run python examples/example.py

Re-run after editing or deleting an object to see the re-sync and forget-on-delete.
"""

import asyncio
import os

import cognee

from cognee_community_connector_s3 import s3_source

# Keep S3 in its own dataset so it is easy to inspect and forget.
DATASET_NAME = "s3"

REQUIRED_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "S3_BUCKET", "S3_PREFIX")


async def main() -> None:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        print(f"Set {', '.join(missing)} to run this example.")
        return

    # Credentials and region are read from the standard AWS_* env vars.
    source = s3_source(
        bucket=os.environ["S3_BUCKET"],
        prefix=os.environ["S3_PREFIX"],
        max_objects=500,
    )

    print("Syncing S3 objects into cognee ...")
    await cognee.remember(source, dataset_name=DATASET_NAME, write_disposition="merge")

    answer = await cognee.search(
        query_text="Summarize what these documents are about.",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=[DATASET_NAME],
    )
    print("\nSearch result:\n", answer)

    print(
        "\nEdit or delete an object under the prefix, then re-run: changed objects "
        "re-sync and deleted ones are reconciled out of memory."
    )


if __name__ == "__main__":
    asyncio.run(main())
