# S3 document connector (initial safe-snapshot implementation)

Uses boto3's standard IAM credential chain. Provide an IAM role, AWS profile,
or environment credentials with least-privilege `s3:ListBucket` for the selected
prefix and `s3:GetObject` for selected objects. Do not commit credentials.

Install from this package directory with `pip install -e .`.
Use `s3_source(bucket="my-bucket", prefix="docs/")` as a document-mode dlt
source with Cognee. See `examples/example.py`.

The Cognee dlt source currently uses a complete snapshot and dlt replace,
so it downloads eligible files each run. The separate `prepare_sync` API
can reuse unchanged content using a scoped local manifest. Its returned pending
manifest must only be committed with `save_manifest` after successful downstream
ingestion; this publication boundary is NOT yet wired into `s3_source`. It supports UTF-8
.txt/.md/.csv/.json, bounded object counts and file sizes. Unsupported types are
not ingested. A failed listing, download, decoding, or exceeded limit aborts
without returning a partial snapshot. Verify Cognee orphan cleanup behavior
before relying on deletion propagation in production.
