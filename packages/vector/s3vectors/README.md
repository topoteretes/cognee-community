# Amazon S3 Vectors Adapter for Cognee

This adapter provides integration between Cognee and [Amazon S3 Vectors](https://aws.amazon.com/s3/features/vectors/), AWS's serverless vector store: no cluster to run, usage-based pricing, and native metadata filtering.

## Features

- Vector storage and search via S3 Vectors' `PutVectors` / `QueryVectors` operations
- Server-side metadata filtering: `node_name` filtering compiles to S3 Vectors' native filter DSL (`$in` / `$and`), so no client-side over-fetching is needed
- Cosine distance, returned in the direction cognee's `ScoredResult` contract expects (lower = more similar)
- Batched writes/deletes (500 vectors per call) and batched retrieval (100 keys per call)
- Async/await support for all operations
- Backend access control support: a dataset database handler is registered, giving each dataset its own vector bucket

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

## Connection Setup

1. Have an AWS account with S3 Vectors available in your region.
2. Provide credentials through the standard AWS chain: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` environment variables, shared config (`~/.aws/credentials`, `AWS_PROFILE`), or an IAM role. S3 Vectors authenticates with IAM (SigV4); there is no separate API key mechanism.
3. Set the region through `AWS_REGION` (or `AWS_DEFAULT_REGION`); it defaults to `us-east-1`.

Alternatively, credentials can be kept in cognee's config:

```dotenv
VECTOR_DB_PROVIDER="s3vectors"
VECTOR_DB_USERNAME="<aws access key id>"   # optional; must be set together with the secret
VECTOR_DB_KEY="<aws secret access key>"    # optional; VECTOR_DB_PASSWORD is an accepted alias
VECTOR_DB_NAME="my-vector-bucket"          # optional; defaults to the cognee database name
VECTOR_DB_URL=""                           # optional custom endpoint (VPC endpoint, etc.)
```

When `VECTOR_DB_USERNAME` / `VECTOR_DB_KEY` are not set, the standard AWS credential chain takes over.

The adapter creates the vector bucket and the per-collection indexes automatically on first write, so the IAM principal needs the permissions below. Set `VECTOR_DB_NAME` (or a `vector_bucket_name`) that is unique within your account and region.

### Required IAM permissions

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3vectors:CreateVectorBucket", "s3vectors:GetVectorBucket",
        "s3vectors:CreateIndex", "s3vectors:GetIndex", "s3vectors:ListIndexes", "s3vectors:DeleteIndex",
        "s3vectors:PutVectors", "s3vectors:GetVectors", "s3vectors:DeleteVectors",
        "s3vectors:QueryVectors"
      ],
      "Resource": "*"
    }
  ]
}
```

Note that querying with a metadata filter (or requesting vector metadata) requires both `s3vectors:QueryVectors` and `s3vectors:GetVectors`.

## Usage

Import and register the adapter in your code:

```python
from cognee_community_vector_adapter_s3vectors import register
```

Also, specify the dataset handler in the .env file:

```dotenv
VECTOR_DATASET_DATABASE_HANDLER="s3vectors"
```

## Example

See example in `example.py` file.

## Key Differences from Other Vector Databases

1. **Collections as indexes**: what other vector databases call "collections" are vector indexes inside a vector bucket; the adapter uses one bucket per instance and one index per cognee collection.
2. **Scoring**: S3 Vectors returns the cosine *distance* (`1 - cosine_similarity`, range `[0, 2]`, lower = more similar), which matches cognee's `ScoredResult` contract directly.
3. **No hybrid search**: S3 Vectors only performs vector similarity search; text queries are embedded first and then run as vector queries.
4. **Payload storage**: the data-point payload is stored in a single non-filterable metadata key (`payload`, JSON string). Filterable metadata is limited to 2 KB per vector, while the payload can use the total 40 KB budget; filters only ever need `belongs_to_set`.

## Service Limits Used by the Adapter

- Up to 500 vectors per put/delete request and 100 keys per get request (the adapter batches automatically)
- Up to 10,000 results per query, paged at 100 results per response (the adapter follows pagination)
- Vector index dimension: 1-4096; distance metric fixed to `cosine`, data type `float32`
