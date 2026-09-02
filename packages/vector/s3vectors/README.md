# Amazon S3 Vectors Adapter for Cognee

This adapter provides integration between Cognee and [Amazon S3 Vectors](https://aws.amazon.com/s3/features/vectors/)
(native vector search on S3, GA 2026) for vector storage and retrieval operations.

## Features

- Full vector search capabilities using S3 Vectors' native `SearchVectors` API
- Server-side metadata filtering via S3 Vectors' filter DSL (`$eq`, `$in`, `$and`, `$or`, etc.)
- Async support for all operations (S3 Vectors' boto3 client is sync; calls are wrapped via `asyncio.to_thread`)
- Batch write support (respects the 500-vectors-per-request limit)

## Installation

If published, the package can be simply installed via pip:

```bash
pip install cognee-community-vector-adapter-s3vectors
```

In case it is not published yet, you can use poetry to locally build the adapter package:

```bash
pip install poetry
poetry install # run this command in the directory containing the pyproject.toml file
```

## Configuration

The adapter requires the following:
- `vector_bucket_name`: the S3 vector bucket to use (created automatically if it doesn't exist)
- `embedding_engine`: an instance of `EmbeddingEngine` for text vectorization
- `region_name` (optional): the AWS region to use
- `distance_metric` (optional): `"cosine"` (default) or `"euclidean"`

AWS credentials are resolved via the standard boto3 credential chain (environment
variables, shared credentials file, IAM role, etc.) — no credentials are passed
directly to the adapter.

## Usage

```python
from cognee.infrastructure.databases.vector.embeddings.EmbeddingEngine import EmbeddingEngine
from packages.vector.s3vectors import S3VectorsAdapter

# Initialize the adapter
embedding_engine = EmbeddingEngine(...)  # Your embedding engine
adapter = S3VectorsAdapter(
    vector_bucket_name="my-vector-bucket",
    embedding_engine=embedding_engine,
    region_name="us-east-1",
)

# Create a collection (vector index)
await adapter.create_collection("my_collection")

# Add data points
await adapter.create_data_points("my_collection", data_points)

# Search
results = await adapter.search(collection_name="my_collection", query_text="search query", limit=10)

# Batch search
results = await adapter.batch_search(
    collection_name="my_collection", query_texts=["query1", "query2"], limit=10
)
```

## Key Differences from Other Vector Databases

1. **Collections as Indexes**: cognee "collections" map to S3 Vectors "indexes." All indexes for
   one adapter instance live in a single vector bucket.
2. **Native metadata filtering**: unlike stores that require client-side filtering, S3 Vectors
   supports a real filter DSL server-side (`$eq`/`$ne`/`$gt`/`$in`/`$and`/`$or`/etc.) — `node_name`
   filters compile directly to this, with no over-fetch-and-filter workaround needed.
3. **Score semantics**: S3 Vectors' `distance` value is already lower-is-better (for `cosine`,
   `distance = 1 - cosine_similarity`; for `euclidean`, the raw Euclidean distance) — matching
   cognee's `ScoredResult` contract directly, with no inversion needed.
4. **Operational limits**: `topK` is capped at 100 per query; writes are capped at 500 vectors per
   `PutVectors` call (the adapter batches automatically); filterable metadata is capped at 2 KB per
   vector, though it also enforces a 40 KB total-metadata-per-vector limit (see
   [Limitations and restrictions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html)).
5. **Permissions**: reading metadata or applying a filter in `search()` requires BOTH
   `s3vectors:QueryVectors` and `s3vectors:GetVectors` IAM permissions — `s3vectors:QueryVectors`
   alone is only sufficient for a bare similarity query with no filter/metadata. See the IAM policy
   example below.

## IAM Permissions

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3vectors:CreateVectorBucket",
        "s3vectors:GetVectorBucket",
        "s3vectors:CreateIndex",
        "s3vectors:GetIndex",
        "s3vectors:ListIndexes",
        "s3vectors:DeleteIndex",
        "s3vectors:PutVectors",
        "s3vectors:GetVectors",
        "s3vectors:QueryVectors",
        "s3vectors:DeleteVectors"
      ],
      "Resource": "*"
    }
  ]
}
```

For production, scope the `Resource` down to specific vector bucket/index ARNs.
