import asyncio
import os

import cognee
from cognee_community_connector_s3 import s3_source


async def main():
    source = s3_source(bucket=os.environ["S3_BUCKET"], prefix=os.environ["S3_PREFIX"])
    await cognee.remember(
        source,
        dataset_name="s3-docs",
        write_disposition="merge",
        max_rows_per_table=0,
    )


if __name__ == "__main__":
    asyncio.run(main())
