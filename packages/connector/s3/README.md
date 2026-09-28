# cognee-community-connector-s3

An AWS S3 data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync the text objects under a bucket prefix into memory ("ask my S3 docs").

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Objects are
decoded as text and ingested as **normal documents** (they flow through cognee's cognify
entity-extraction pipeline, not the deterministic dlt-row path), via cognee's document-mode
marker, the same as the [Notion connector](../notion/README.md).

## Requirements

- cognee **1.4.0** (the first release with document-mode: `DOCUMENT_SOURCE_ATTR` and the
  `resolve_dlt_sources` routing that reads it).
- Python 3.11–3.13.

## Install

```bash
uv pip install cognee-community-connector-s3
# or, from this monorepo:
cd packages/connector/s3 && uv sync --all-groups
```

## Setup

1. Create an IAM user or role that can read the prefix. A minimal policy:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "s3:ListBucket",
         "Resource": "arn:aws:s3:::<your-bucket>",
         "Condition": { "StringLike": { "s3:prefix": "docs/*" } }
       },
       {
         "Effect": "Allow",
         "Action": "s3:GetObject",
         "Resource": "arn:aws:s3:::<your-bucket>/docs/*"
       }
     ]
   }
   ```

2. Provide its credentials, either as arguments or through the standard AWS environment
   variables (never hardcode them):

   ```bash
   export AWS_ACCESS_KEY_ID="<your-access-key-id>"
   export AWS_SECRET_ACCESS_KEY="<your-secret-access-key>"
   export AWS_SESSION_TOKEN="<optional-session-token>"   # temporary STS credentials only
   export AWS_REGION="us-east-1"
   ```

   plus your `LLM_API_KEY`, as for any other cognee run.

## Usage

```python
import cognee
from cognee_community_connector_s3 import s3_source

await cognee.remember(
    s3_source(bucket="<your-bucket>", prefix="docs/"),  # credentials from AWS_* env vars
    dataset_name="s3",
    write_disposition="merge",  # incremental: only new/changed objects are downloaded
)

answer = await cognee.search(
    query_text="Summarize these documents.",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["s3"],
)
```

## Configuration

| Argument | Default | Env fallback | Notes |
| --- | --- | --- | --- |
| `bucket` | required | | Bucket to read from. |
| `prefix` | required | | Key prefix to sync, e.g. `"docs/"`. Required so a sync never covers a whole bucket by accident. |
| `aws_access_key_id` | `None` | `AWS_ACCESS_KEY_ID` | Required (argument or env). |
| `aws_secret_access_key` | `None` | `AWS_SECRET_ACCESS_KEY` | Required (argument or env). |
| `aws_session_token` | `None` | `AWS_SESSION_TOKEN` | For temporary (STS) credentials. |
| `region_name` | `None` | `AWS_REGION`, `AWS_DEFAULT_REGION` | Bucket region. |
| `endpoint_url` | `None` | | S3-compatible endpoints (MinIO, LocalStack, ...). |
| `max_objects` | `1000` | | The sync **aborts before downloading anything** if the prefix lists more objects than this. |
| `max_object_size` | `10 MiB` | | Larger objects are skipped (and forgotten if they were ingested before). |
| `client` | `None` | | A pre-built boto3 S3 client, mainly for tests. |

Only text-like objects are ingested: `.txt .md .markdown .json .csv .xml .yaml .yml .log .html
.htm .rst`, decoded as UTF-8. Other types (PDFs, images, ...) are skipped, and "folder"
placeholder keys ending in `/` are ignored.

## How sync + forget-on-delete work

Listing a prefix is cheap and complete, so the source keeps a per-object cursor in dlt resource
state: each key's `LastModified`, tie-broken by its ETag because S3 reports `LastModified` at
one-second resolution. The cursor is namespaced by `bucket/prefix`.

- **With `write_disposition="merge"` (recommended)**, each run lists the prefix and compares it
  with the cursor:
  - new or changed objects are downloaded and upserted;
  - unchanged objects are neither downloaded nor re-ingested;
  - keys that disappeared (deleted, or now too large) are emitted as tombstones whose
    `hard_delete` column makes dlt remove the row from staging.

  cognee then reads back the whole staging table, and its existing `orphan_cleanup` removes the
  missing objects from the graph and vector stores, the same final step as the Notion connector.
- **With any other disposition** (cognee defaults to `replace`), the source takes a **full
  snapshot**, exactly like Notion: every eligible object is downloaded and staging is rewritten,
  so deleted objects drop out. This fallback is deliberate: yielding only changed objects under
  `replace` would wipe unchanged objects from staging and forget them. The cursor is still
  recorded, so switching to `merge` later starts from an accurate baseline.

In both modes, unchanged objects keep a stable content-hash `data_id`, so they are not
re-cognified.

Safety and caveats:

- Any listing or download error aborts the run. dlt only commits the cursor with a successful
  load, so staging, the cursor and memory stay untouched and the next run retries. An object
  deleted between listing and download is treated as deleted.
- If the dlt pipeline state is lost (cognee keeps it under `~/.dlt/pipelines`), the next `merge`
  run downloads everything again and cannot see objects deleted before the loss. A single
  `replace` run resynchronises.
- cognee skips orphan cleanup when a sync reads back zero rows (it can't tell an empty prefix
  from a broken sync), so deleting *every* object under the prefix does not clear memory. Use
  `cognee.prune` or a dedicated dataset for that.

### Why not cognee's S3 storage or loaders?

cognee's `S3FileStorage` is an async s3fs wrapper for cognee's own file storage. It doesn't
expose `LastModified` and can't be driven from dlt's synchronous resource generator, so this
connector talks to S3 directly through boto3's `list_objects_v2` paginator. For the same reason,
and like the Notion connector, it extracts text itself instead of calling cognee's async,
path-based loaders.

## Example

`examples/example.py` runs the full flow. It reads everything from environment variables and
exits with a message if any are missing:

```bash
export AWS_ACCESS_KEY_ID="<your-access-key-id>"
export AWS_SECRET_ACCESS_KEY="<your-secret-access-key>"
export AWS_REGION="us-east-1"
export S3_BUCKET="<your-bucket>"
export S3_PREFIX="docs/"
export LLM_API_KEY="<your-llm-api-key>"
uv run python examples/example.py
```

Re-run it after editing or deleting an object to see the incremental re-sync and
forget-on-delete.

## Testing

```bash
uv run pytest tests/
```

The tests run entirely against [moto](https://github.com/getmoto/moto)'s `mock_aws` and make no
real AWS calls. `tests/conftest.py` sets fake credentials and points
`AWS_SHARED_CREDENTIALS_FILE`/`AWS_CONFIG_FILE` at a non-existent file, so local `~/.aws`
credentials can never be picked up. The tests cover listing and the guardrails, first ingest,
the incremental cursor (only changed objects are downloaded), forget-on-delete, per-prefix
cursor scoping, the `replace` full-snapshot fallback, and aborting on errors.
