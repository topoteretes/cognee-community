"""Opt-in S3 → Cognee document ingestion example.

Requires configured boto3 credentials and Cognee LLM settings.
No AWS requests occur until main() runs.
"""
import asyncio
import os

import cognee
from cognee_community_connector_s3 import s3_source


async def main():
    bucket = os.environ.get("COGNEE_S3_BUCKET")
    if not bucket:
        raise SystemExit("Set COGNEE_S3_BUCKET before running this example")
    prefix = os.environ.get("COGNEE_S3_PREFIX", "docs/")
    await cognee.remember(
        s3_source(bucket=bucket, prefix=prefix),
        dataset_name="s3_documents",
    )


if __name__ == "__main__":
    asyncio.run(main())
