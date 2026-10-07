# cognee-community-connector-s3

AWS S3 data-source connector for cognee. It synchronizes text documents from one explicit bucket/prefix into a dedicated cognee dataset.

## Safety model

The connector treats S3 reconciliation as an authority boundary, not as a file loop. It lists the complete selected prefix before making deletion decisions. A partial or failed listing, an exhausted object limit, or a changed/unreadable object aborts the sync. Objects that are present but unsupported or over the configured size limit are **not** interpreted as deleted. Reads use the listed ETag as an `IfMatch` precondition so content cannot silently change between inventory and download.

Use a dedicated dataset and foreground ingestion. Current cognee document-source cleanup tracks successfully loaded tables even when hard-delete tombstones empty the table, so deleting the final upstream object is reconciled out of memory rather than left behind.

## Install

```bash
uv pip install cognee-community-connector-s3
```

## IAM

Prefer an IAM role or the standard boto3 credential chain. Grant only `s3:ListBucket` on the selected bucket/prefix and `s3:GetObject` on the selected objects. Do not hardcode keys in code or examples.

## Usage

```python
import cognee
from cognee_community_connector_s3 import s3_source

source = s3_source(bucket="my-bucket", prefix="docs/")
await cognee.remember(
    source,
    dataset_name="s3-docs",
    write_disposition="merge",
    max_rows_per_table=0,
)
```

Supported text extensions are txt, md/markdown, json, csv, xml, yaml/yml, log, html/htm and rst. `max_objects` defaults to 1000 and `max_object_size` to 10 MiB.

## Verification

Tests use moto with fake credentials and no real AWS access. They cover bounded inventory, incremental/no-op sync, changed objects, final-object deletion, present-but-skipped objects, and read/list failure boundaries.
