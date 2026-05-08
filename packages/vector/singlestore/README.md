# Cognee SingleStore Adapter

SingleStore vector database adapter for Cognee. Cognee remains the memory and orchestration layer; SingleStore stores embeddings, payload metadata, and dataset-scoped vector rows.

The adapter uses SingleStore's Python driver, `singlestoredb`, and SingleStore vector SQL (`VECTOR`, `DOT_PRODUCT`, and JSON functions).

## Installation

If published, install the package with pip:

```bash
pip install cognee-community-vector-adapter-singlestore
```

For local development, install from this package directory:

```bash
pip install poetry
poetry install
```

Older Poetry versions may require the `[tool.poetry]` metadata already included in this package.

## Register The Adapter

Import the `register` module before using Cognee so the provider is available:

```python
from cognee_community_vector_adapter_singlestore import register  # noqa: F401
```

Configure Cognee:

```python
import os

from cognee import config

os.environ.setdefault("VECTOR_DATASET_DATABASE_HANDLER", "singlestore")

config.set_vector_db_config(
    {
        "vector_db_provider": "singlestore",
        "vector_db_url": os.getenv("SINGLESTORE_URL", "127.0.0.1:3306"),
        "vector_db_key": os.getenv("SINGLESTORE_KEY", ""),
    }
)
```

## SingleStore Local Docker

Start the SingleStore dev image and create a physical database for Cognee:

```bash
docker run -d --name singlestoredb-dev \
  -e ROOT_PASSWORD="test" \
  -p 3306:3306 -p 8080:8080 -p 9000:9000 \
  ghcr.io/singlestore-labs/singlestoredb-dev:latest

mysql -h 127.0.0.1 -P 3306 -u root -ptest \
  -e "CREATE DATABASE IF NOT EXISTS cognee"
```

The adapter does not create `VECTOR_DB_NAME`. It connects to that database when the vector engine starts, so create it before running Cognee.

Set SingleStore connection variables:

```bash
export VECTOR_DB_PROVIDER=singlestore
export VECTOR_DATASET_DATABASE_HANDLER=singlestore

export VECTOR_DB_HOST=127.0.0.1
export VECTOR_DB_PORT=3306
export VECTOR_DB_NAME=cognee
export VECTOR_DB_USERNAME=root
export VECTOR_DB_PASSWORD=test

export SINGLESTORE_URL=127.0.0.1:3306
export SINGLESTORE_KEY="$VECTOR_DB_PASSWORD"
```

## SingleStore Cloud

Create a database in your workspace, then use the Cloud endpoint, username, and password:

```sql
CREATE DATABASE IF NOT EXISTS cognee;
```

```bash
export VECTOR_DB_PROVIDER=singlestore
export VECTOR_DATASET_DATABASE_HANDLER=singlestore

export VECTOR_DB_HOST="<workspace-host>"
export VECTOR_DB_PORT=3306
export VECTOR_DB_NAME=cognee
export VECTOR_DB_USERNAME="<username>"
export VECTOR_DB_PASSWORD="<password>"

export SINGLESTORE_URL="$VECTOR_DB_HOST:$VECTOR_DB_PORT"
export SINGLESTORE_KEY="$VECTOR_DB_PASSWORD"
```

You can run the database creation statement from the SingleStore Cloud SQL editor or any SQL client connected with a user that has database creation privileges.

If your environment requires SSL options, pass a JSON `VECTOR_DB_KEY` / `SINGLESTORE_KEY` once adapter support for those options is extended. Do not commit credentials or customer data in `.env` files.

## SingleStore Environment Variables

```dotenv
VECTOR_DB_PROVIDER=singlestore
VECTOR_DATASET_DATABASE_HANDLER=singlestore

# Either use a URL:
VECTOR_DB_URL=127.0.0.1:3306

# Or individual connection fields:
VECTOR_DB_HOST=127.0.0.1
VECTOR_DB_PORT=3306
VECTOR_DB_NAME=cognee
VECTOR_DB_USERNAME=root
VECTOR_DB_PASSWORD=test

# Optional Cognee-owned table prefix.
COGNEE_SINGLESTORE_TABLE_PREFIX=cognee_vec_

# Optional. Enabled by default so DOT_PRODUCT behaves as cosine similarity.
COGNEE_SINGLESTORE_NORMALIZE_VECTORS=true
```

`VECTOR_DB_NAME` is the physical SingleStore database. Cognee's dataset database handler stores each Cognee dataset using a separate vector engine `database_name`, and the adapter writes that value to a `database_name` column and payload metadata for row-level isolation.

The adapter does not create the physical `VECTOR_DB_NAME` database. Create it first:

```bash
mysql -h "$VECTOR_DB_HOST" -P "$VECTOR_DB_PORT" -u "$VECTOR_DB_USERNAME" -p \
  -e "CREATE DATABASE IF NOT EXISTS \`$VECTOR_DB_NAME\`"
```

`VECTOR_DB_KEY` / `SINGLESTORE_KEY` may be a raw password or JSON:

```json
{"username":"root","password":"test","database":"cognee"}
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

### AWS Bedrock Native API

Install the Bedrock dependency in the Poetry environment:

```bash
poetry run pip install "boto3>=1.34,<2" "botocore[crt]"
```

Use AWS credentials or a configured profile:

```bash
export AWS_REGION=us-east-1
# export AWS_PROFILE=<profile-name>
# or:
# export AWS_ACCESS_KEY_ID=<access-key>
# export AWS_SECRET_ACCESS_KEY=<secret-key>
# export AWS_SESSION_TOKEN=<session-token-if-needed>
```

For Cognee structured extraction, Claude Haiku 3.5 worked well in testing:

```bash
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

Some Bedrock models require inference profile IDs such as `us.anthropic.claude-3-5-haiku-20241022-v1:0`. If a bare model ID fails with an on-demand throughput error, list available profiles:

```bash
aws bedrock list-inference-profiles \
  --region us-east-1 \
  --query 'inferenceProfileSummaries[].inferenceProfileId' \
  --output text
```

### OpenAI-Compatible APIs

For an OpenAI-compatible endpoint, including Bedrock Mantle, set Cognee's OpenAI fields:

```bash
export LLM_PROVIDER=openai
export LLM_MODEL="<model-id-from-/models>"
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

Plain chat support is not enough for Cognee; the endpoint/model must also work with Cognee's `instructor` structured-output path. If graph extraction hangs or schema validation fails, use a model with stronger structured JSON behavior or switch to native Bedrock with Claude.

## Schema

The adapter creates one SingleStore table per Cognee collection using the configured prefix:

```sql
CREATE TABLE cognee_vec_<collection> (
  database_name VARCHAR(255) NOT NULL,
  id VARCHAR(64) NOT NULL,
  payload JSON,
  vector VECTOR(<embedding_dim>, F32) NOT NULL,
  created_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
  updated_at DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (database_name, id),
  SHARD KEY (database_name, id),
  SORT KEY ()
);
```

Search uses exact `DOT_PRODUCT` scoring:

```sql
SELECT id, payload, DOT_PRODUCT(vector, @query_vector) AS score
FROM cognee_vec_<collection>
WHERE database_name = ?
ORDER BY score DESC
LIMIT ?;
```

Vectors are normalized before insert and query by default. If your embedding model already returns normalized vectors, this should not change direction or recall. Set `COGNEE_SINGLESTORE_NORMALIZE_VECTORS=false` to disable it.

## Filtering And Pruning

- `belongs_to_set` filters are evaluated from `payload` with SingleStore `JSON_MATCH_ANY`.
- Searches, retrievals, deletes, collection listing, and prune operations are scoped by `database_name`.
- `prune()` deletes only rows for the current Cognee dataset and drops only prefixed tables that become empty.
- No credentials or raw customer/export data are ingested by default.

## Run The Example

After SingleStore and LLM/embedding credentials are configured:

```bash
export COGNEE_SKIP_CONNECTION_TEST=true
export ENABLE_BACKEND_ACCESS_CONTROL=false

poetry run python example.py
```

Inspect SingleStore during or after a run:

```bash
mysql -h 127.0.0.1 -P 3306 -u root -p -D cognee \
  -e "SHOW TABLES LIKE 'cognee_vec_%';"
```

## Tests

Run adapter-only lifecycle and dataset handler tests without live LLM calls:

```bash
poetry run python - <<'PY'
import asyncio
from tests.test_singlestore import (
    test_adapter_lifecycle_and_filters,
    test_dataset_handler_metadata_and_delete,
)

async def main():
    await test_adapter_lifecycle_and_filters()
    await test_dataset_handler_metadata_and_delete()
    print("adapter + dataset handler tests passed")

asyncio.run(main())
PY
```

Run the full integration test:

```bash
poetry run python tests/test_singlestore.py
```

The full test expects:

- A reachable SingleStore database.
- Working LLM and embedding provider configuration.
- `VECTOR_DATASET_DATABASE_HANDLER=singlestore`.

## Troubleshooting

- `LLM connection test timed out`: verify API reachability, or set `COGNEE_SKIP_CONNECTION_TEST=true` for local testing.
- OpenAI quota errors: switch to a funded provider such as Bedrock.
- Bedrock `MissingDependencyException` for login credentials: install `poetry run pip install "botocore[crt]"`.
- Bedrock `on-demand throughput isn't supported`: use an inference profile ID such as `us.anthropic.claude-3-5-haiku-20241022-v1:0`.
- OpenAI-compatible endpoint works with `curl` but Cognee hangs: the model may not support `instructor` structured output reliably. Use native Bedrock Claude or a stronger JSON-structured model.
- `tiktoken` cannot map a non-OpenAI embedding model: do not set `EMBEDDING_PROVIDER=openai` for Titan or other non-OpenAI embeddings. Use `EMBEDDING_PROVIDER=bedrock`.
