# cognee-community-connector-s3

AWS S3 data-source connector for [cognee](https://github.com/topoteretes/cognee).
It synchronizes UTF-8 text documents from one explicit bucket/prefix into Cognee
memory with incremental updates and fail-closed deletion reconciliation.

## Requirements

- Python 3.11-3.13
- Cognee 1.6.1 or newer
- read access to the selected S3 bucket/prefix

Cognee 1.6.1 introduced table-scoped document cleanup that can reconcile a
confirmed empty source table. The connector refuses older Cognee builds rather
than risk incomplete final-object deletion.

## Install

From this monorepo:

```bash
cd packages/connector/s3
pip install -e .
```

When published:

```bash
pip install cognee-community-connector-s3
```

## IAM

The connector uses boto3's normal credential chain. Prefer an IAM role, workload
identity, or profile instead of static keys.

Grant only:

- `s3:ListBucket` for the selected bucket/prefix
- `s3:GetObject` for objects under that prefix

The connector does not persist or log credentials.

## Usage

```python
import cognee
from cognee_community_connector_s3 import s3_source

source = s3_source(
    bucket="my-bucket",
    prefix="docs/",
)

await cognee.remember(
    source,
    dataset_name="s3-docs",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```

A non-empty prefix is intentionally required. Whole-bucket synchronization must
be an explicit design decision rather than an accidental scope expansion.

## Reconciliation model

The connector treats deletion as an authority boundary:

1. It paginates the complete selected prefix before authorizing any deletion.
2. `LastModified + ETag + Size` are change signals; unchanged objects are not
   downloaded again.
3. Changed objects are read with an ETag precondition so a list/read race fails
   the run instead of publishing stale content.
4. Listing failure, object-limit exhaustion, invalid metadata, read failure,
   version race, invalid UTF-8, or an object that grows past the configured
   limit aborts the run.
5. Present-but-unsupported or oversized objects are not reclassified as
   deleted.
6. A hard-delete tombstone is emitted only when a previously ingested key is
   absent from a successfully completed inventory.
7. Each bucket/prefix receives a deterministic dlt table and pipeline scope, so
   synchronizing one prefix cannot clean up another prefix in the same Cognee
   dataset.

The dlt resource checkpoint is updated only after the generator has completed.
If extraction fails, the checkpoint is not advanced. If destination loading fails
after extraction, dlt retains the pending load package and loads it on the next
run before new extraction, so a changed object is not silently skipped.

## Supported content and limits

Supported extensions:

`txt`, `md`, `markdown`, `json`, `csv`, `xml`, `yaml`, `yml`,
`log`, `html`, `htm`, `rst`

Content must be valid UTF-8 text.

Defaults:

- `max_objects=1000`
- `max_object_size=10 MiB`

Increase limits deliberately for larger scopes.

## Verification

```bash
pytest -q
ruff format --check cognee_community_connector_s3 tests examples
ruff check cognee_community_connector_s3 tests examples
```

The automated suite uses Moto and fake AWS credentials. It covers the boto3
credential chain, real pagination beyond 1,000 objects, prefix isolation,
incremental/no-op sync, overwrite detection, changed-object replay after read
failure, downstream-load replay, partial-read failure, listing failure, ETag
races, invalid UTF-8, object and folder-marker limits, URL-safe stable identity,
present-but-oversized objects, final-object deletion, Cognee Data-record
reconciliation, cleanup isolation between two prefixes in one dataset, and
cognified graph cleanup after deletion.

The unit suite is also run against the minimum supported Cognee 1.6.1, not only
the current 1.6.4 release.

No live AWS credentials are required for the automated tests. The connector was
also smoke-tested read-only against a public AWS S3 bucket, including a second
no-op run that reused all previously committed objects. Write/delete behavior
is exercised against Moto; an authenticated live write/delete smoke test should
use a disposable prefix and the same least-privilege policy.