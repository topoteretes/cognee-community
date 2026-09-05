# Cognee ClickHouse Adapter

ClickHouse vector database adapter for Cognee. Cognee remains the memory and orchestration layer; ClickHouse stores embeddings, payload metadata, and dataset-scoped vector rows.

The adapter uses the official `clickhouse-connect` Python client, ClickHouse `Array(Float32)` vectors, exact `dotProduct` search, and optional ClickHouse `vector_similarity` indexes for approximate cosine search on ClickHouse 25.8+.

## Installation

If published, install the package with pip:

```bash
pip install cognee-community-vector-adapter-clickhouse
```

For local development, install from this package directory:

```bash
pip install poetry
poetry install
```

## Register The Adapter

Import the `register` module before using Cognee so the provider is available:

```python
from cognee_community_vector_adapter_clickhouse import register  # noqa: F401
```

Configure Cognee:

```python
import os

from cognee import config

os.environ.setdefault("VECTOR_DATASET_DATABASE_HANDLER", "clickhouse")

config.set_vector_db_config(
    {
        "vector_db_provider": "clickhouse",
        "vector_db_url": os.getenv("CLICKHOUSE_URL", "http://127.0.0.1:8123/cognee"),
        "vector_db_key": os.getenv("CLICKHOUSE_KEY", ""),
    }
)
```

## Local Docker

Start ClickHouse and create the physical database used by the adapter:

```bash
docker run -d --name clickhouse-cognee \
  -e CLICKHOUSE_DB=cognee \
  -p 8123:8123 -p 9000:9000 \
  clickhouse/clickhouse-server:25.8
```

Set connection variables:

```bash
export VECTOR_DB_PROVIDER=clickhouse
export VECTOR_DATASET_DATABASE_HANDLER=clickhouse

export VECTOR_DB_URL=http://127.0.0.1:8123/cognee
export VECTOR_DB_NAME=cognee
export CLICKHOUSE_HOST=127.0.0.1
export CLICKHOUSE_PORT=8123
export CLICKHOUSE_DATABASE=cognee
export CLICKHOUSE_USERNAME=default
export CLICKHOUSE_PASSWORD=
export CLICKHOUSE_SECURE=false
```

The adapter does not create the physical ClickHouse database outside Docker's `CLICKHOUSE_DB` bootstrap path. For existing servers, create it first:

```bash
curl -sS "http://127.0.0.1:8123/" --data-binary "CREATE DATABASE IF NOT EXISTS cognee"
```

## ClickHouse Cloud

ClickHouse Cloud uses HTTPS on port 8443:

```bash
export VECTOR_DB_PROVIDER=clickhouse
export VECTOR_DATASET_DATABASE_HANDLER=clickhouse

export CLICKHOUSE_HOST="<service-host>.clickhouse.cloud"
export CLICKHOUSE_PORT=8443
export CLICKHOUSE_DATABASE=cognee
export CLICKHOUSE_USERNAME=default
export CLICKHOUSE_PASSWORD="<password>"
export CLICKHOUSE_SECURE=true
```

Do not commit credentials or customer data in `.env` files.

## Environment Variables

```dotenv
VECTOR_DB_PROVIDER=clickhouse
VECTOR_DATASET_DATABASE_HANDLER=clickhouse

# Either use a URL:
VECTOR_DB_URL=http://127.0.0.1:8123/cognee

# Or individual fields (fallbacks if CLICKHOUSE_URL is not set):
CLICKHOUSE_HOST=127.0.0.1
CLICKHOUSE_PORT=8123
CLICKHOUSE_DATABASE=cognee
CLICKHOUSE_USERNAME=default
CLICKHOUSE_PASSWORD=
CLICKHOUSE_SECURE=false

# Advanced TLS / connection options
# CLICKHOUSE_COMPRESS=false
# CLICKHOUSE_VERIFY=true
# CLICKHOUSE_CA_CERT=
# CLICKHOUSE_CLIENT_CERT=
# CLICKHOUSE_CLIENT_CERT_KEY=
# CLICKHOUSE_CONNECT_TIMEOUT=10
# CLICKHOUSE_SEND_RECEIVE_TIMEOUT=300

# Optional Cognee-owned table prefix.
COGNEE_CLICKHOUSE_TABLE_PREFIX=cognee_vec_

# Optional. Enabled by default so dotProduct behaves like cosine similarity.
COGNEE_CLICKHOUSE_NORMALIZE_VECTORS=true

# Optional approximate vector index. Requires ClickHouse 25.8+.
COGNEE_CLICKHOUSE_ENABLE_VECTOR_INDEX=false
COGNEE_CLICKHOUSE_VECTOR_INDEX_GRANULARITY=100000000
COGNEE_CLICKHOUSE_VECTOR_INDEX_NAME=idx_vector

# Set explicitly if your embedding model produces a non-standard size.
# EMBEDDING_DIMENSIONS=384
```

`VECTOR_DB_NAME` or `CLICKHOUSE_DATABASE` is the physical ClickHouse database. Cognee's dataset database handler stores each Cognee dataset using a separate vector engine `database_name`; the adapter writes that value to a `database_name` column for row-level dataset isolation.

`VECTOR_DB_KEY` / `CLICKHOUSE_KEY` may be a raw password or JSON:

```json
{"username":"default","password":"secret","database":"cognee","secure":true}
```

## LLM And Embedding Providers

Cognee needs an LLM for graph extraction and an embedding provider for vector storage. Cursor subscription credentials are not used by Cognee; configure provider API credentials in your shell or `.env`.

### OpenAI

```bash
export LLM_PROVIDER=openai
export LLM_MODEL=openai/gpt-4o-mini
export LLM_API_KEY="<openai-api-key>"
unset LLM_ENDPOINT LLM_API_VERSION

export EMBEDDING_PROVIDER=openai
export EMBEDDING_MODEL=openai/text-embedding-3-small
export EMBEDDING_API_KEY="$LLM_API_KEY"
export EMBEDDING_DIMENSIONS=1536
unset EMBEDDING_ENDPOINT EMBEDDING_API_VERSION
```

### Local Embeddings With fastembed (no API key needed)

The `fastembed` provider runs entirely locally using ONNX models downloaded from Hugging Face. No API key is required for embeddings:

```bash
poetry run pip install fastembed

export EMBEDDING_PROVIDER=fastembed
export EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
export EMBEDDING_DIMENSIONS=384
```

Pair with any LLM provider for the graph extraction step.

### OpenAI-Compatible APIs

For any OpenAI-compatible endpoint (e.g. Featherless, local vLLM):

```bash
export LLM_PROVIDER=openai
export LLM_MODEL="openai/<model-id>"
export LLM_API_KEY="<api-key>"
export LLM_ENDPOINT="https://<host>/v1"
export LLM_API_VERSION=""
export LLM_INSTRUCTOR_MODE=json_mode
export LLM_MAX_COMPLETION_TOKENS=2048

export OPENAI_API_KEY="$LLM_API_KEY"
export OPENAI_BASE_URL="$LLM_ENDPOINT"
```

Verify model availability:

```bash
curl -sS "$LLM_ENDPOINT/models" \
  -H "Authorization: Bearer $LLM_API_KEY" | jq -r '.data[].id'
```

Plain chat support is not enough for Cognee; the endpoint/model must also work with Cognee's `instructor` structured-output path. If graph extraction hangs or schema validation fails, use a model with stronger structured JSON behaviour or switch to OpenAI native.

### AWS Bedrock Native API

Install the Bedrock dependency in the Poetry environment:

```bash
poetry run pip install "boto3>=1.34,<2" "botocore[crt]"
```

```bash
export AWS_REGION=us-east-1
# export AWS_PROFILE=<profile-name>

export LLM_PROVIDER=bedrock
export LLM_MODEL=us.anthropic.claude-3-5-haiku-20241022-v1:0
export LLM_INSTRUCTOR_MODE=json_schema_mode
export LLM_MAX_COMPLETION_TOKENS=2048
unset LLM_API_KEY LLM_ENDPOINT LLM_API_VERSION

export EMBEDDING_PROVIDER=bedrock
export EMBEDDING_MODEL=amazon.titan-embed-text-v2:0
export EMBEDDING_DIMENSIONS=1024
unset EMBEDDING_API_KEY EMBEDDING_ENDPOINT EMBEDDING_API_VERSION
```

## Schema

The adapter creates one ClickHouse `ReplacingMergeTree` table per Cognee collection:

```sql
CREATE TABLE cognee_vec_<collection>
(
    database_name  String,
    id             String,
    payload        String,
    text           String,
    belongs_to_set Array(String),
    vector         Array(Float32),
    version        UInt64,
    is_deleted     UInt8
)
ENGINE = ReplacingMergeTree(version, is_deleted)
ORDER BY (database_name, id);
```

Logical deletes write a tombstone row (`is_deleted = 1`) with a higher `version`. All queries use `FINAL` to deduplicate and filter `is_deleted = 0` for correctness. `prune()` issues an `ALTER TABLE … DELETE` for the current dataset's rows and only drops the physical table when no live rows remain across all datasets.

## Search And Indexing

Exact search is always available and is the correctness baseline. The adapter normalises vectors by default and scores exact search with `dotProduct`, so higher scores are better, matching the other Cognee vector adapters.

When `COGNEE_CLICKHOUSE_ENABLE_VECTOR_INDEX=true`, the adapter tries to add and materialise a ClickHouse `vector_similarity('hnsw', cosineDistance, <dimension>)` index. If the server is older than ClickHouse 25.8 or index creation fails, the adapter logs a warning and continues with exact search. ANN search converts cosine distance back to a higher-is-better score with `1 - cosineDistance(...)`.

## Filtering And Pruning

- `belongs_to_set` filters are evaluated with ClickHouse `hasAny` / `hasAll` on the `belongs_to_set` array column.
- Searches, retrievals, deletes, collection listing, and prune operations are scoped by `database_name`.
- `prune()` deletes only rows for the current Cognee dataset and drops only prefixed tables that become fully empty across all datasets.
- No credentials or raw customer/export data are ingested by default.

## Run The Example

After ClickHouse and LLM/embedding credentials are configured:

```bash
export COGNEE_SKIP_CONNECTION_TEST=true
export ENABLE_BACKEND_ACCESS_CONTROL=false

poetry run python example.py
```

Inspect ClickHouse tables during or after a run:

```bash
curl -sS "http://127.0.0.1:8123/" \
  --data-binary "SELECT name FROM system.tables WHERE database = 'cognee' AND name LIKE 'cognee_vec_%'"
```

## Tests

Unit and integration tests (no LLM calls needed) cover connection config, serialisation, vector validation, filter logic, mocked search, adapter lifecycle (create/search/delete/prune), dataset handler, and vector index smoke test. Run them with a live ClickHouse but without setting any LLM credentials:

```bash
poetry run pytest tests/test_clickhouse.py -k "not recall"
```

The full suite including the end-to-end recall flow test:

```bash
poetry run pytest tests/test_clickhouse.py
```

`test_cognee_recall_flow` is automatically skipped when `LLM_API_KEY` or `EMBEDDING_API_KEY` are not set. The full test expects:

- A reachable ClickHouse database.
- Working LLM and embedding provider configuration.
- `VECTOR_DATASET_DATABASE_HANDLER=clickhouse`.

## Troubleshooting

- `LLM connection test timed out`: verify API reachability, or set `COGNEE_SKIP_CONNECTION_TEST=true` for local testing.
- OpenAI quota or rate-limit errors: switch to a lower-concurrency model or a different provider. Models with high per-request concurrency costs (e.g. Kimi-K2 on Featherless) can exhaust plan limits when `cognify()` fires parallel LLM calls; use a smaller model such as `Qwen/Qwen2.5-14B-Instruct`.
- `KeyError: 'Could not automatically map <model> to a tokeniser'`: `EMBEDDING_PROVIDER=openai` uses TikToken which only knows OpenAI model names. Use `EMBEDDING_PROVIDER=openai_compatible` with `EMBEDDING_DIMENSIONS` set explicitly, or `EMBEDDING_PROVIDER=fastembed` for local embeddings.
- Vector dimension mismatch on first write: set `EMBEDDING_DIMENSIONS` explicitly to match your embedding model's output size. The adapter reads this at table-creation time and cannot change it afterwards without dropping the table.
- Bedrock `MissingDependencyException`: install `poetry run pip install "botocore[crt]"`.
- Bedrock `on-demand throughput isn't supported`: use an inference profile ID such as `us.anthropic.claude-3-5-haiku-20241022-v1:0`.
- OpenAI-compatible endpoint works with `curl` but Cognee hangs: the model may not support `instructor` structured output reliably. Use a model with stronger structured JSON behaviour.
