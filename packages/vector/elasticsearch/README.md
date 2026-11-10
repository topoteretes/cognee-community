# Elasticsearch Adapter for Cognee

This adapter stores and searches cognee's embeddings in
[Elasticsearch](https://www.elastic.co/elasticsearch) (8.x or 9.x, self-hosted,
Elastic Cloud, or serverless) using `dense_vector` fields with HNSW and cosine
similarity.

Elasticsearch has no graph model, so this is a **vector** adapter only. Pair it
with any cognee graph backend (the default is the bundled Ladybug/Kuzu engine).

## Features

- Approximate kNN search over HNSW-indexed `dense_vector` fields, with node-set
  filtering (`belongs_to_set`, `OR` and `AND`) applied as a kNN pre-filter;
- Batch search in one `_msearch` round trip;
- Exact neighbourhood re-scoring (`score_by_ids`) through `script_score`;
- Payload-only updates (`update_payload`) and node-set detagging
  (`remove_belongs_to_set_tags`) without re-embedding;
- Raw vector upserts (`upsert_raw_vectors`) for cognee's system-owned vectors;
- Dataset isolation on a shared cluster, plus a dataset database handler for
  cognee's multi-tenant mode.

## Installation

```bash
pip install cognee-community-vector-adapter-elasticsearch
```

Or locally, from this directory:

```bash
uv sync
# OR
poetry install
```

The package depends on `elasticsearch>=8.17,<10`. Install the client major that
matches your cluster (`elasticsearch<9` for an 8.x cluster); the adapter only
uses APIs both majors share.

## Connection Setup

For a quick local cluster without security:

```bash
docker run -d --name cognee-es -p 9200:9200 \
  -e discovery.type=single-node \
  -e xpack.security.enabled=false \
  -e ES_JAVA_OPTS="-Xms1g -Xmx1g" \
  docker.elastic.co/elasticsearch/elasticsearch:9.5.6
```

## Configuration

Set the usual cognee vector variables (see `.env.example`):

| Variable | Meaning |
|----------|---------|
| `VECTOR_DB_PROVIDER` | `elasticsearch` |
| `VECTOR_DB_URL` | One or more node URLs, comma-separated. Falls back to `http://VECTOR_DB_HOST:VECTOR_DB_PORT`, then `http://localhost:9200`. |
| `VECTOR_DB_KEY` | An Elasticsearch API key: the encoded value the API returns, or `<id>:<api_key>`. Takes precedence over basic auth. |
| `VECTOR_DB_USERNAME` / `VECTOR_DB_PASSWORD` | HTTP basic auth, when no API key is set. |
| `VECTOR_DATASET_DATABASE_HANDLER` | `elasticsearch`, to give every cognee dataset its own scope. |

Elasticsearch-specific extras:

| Variable | Meaning |
|----------|---------|
| `ELASTICSEARCH_CLOUD_ID` | Elastic Cloud deployment id; replaces `VECTOR_DB_URL`. |
| `ELASTICSEARCH_INDEX_PREFIX` | Prefix of every index the adapter manages (default `cognee`). `prune()` and collection listing only ever touch `<prefix>-*`. |
| `ELASTICSEARCH_VERIFY_CERTS` / `ELASTICSEARCH_CA_CERTS` | TLS settings for self-signed clusters. |

## Usage

Import and register the adapter before using cognee:

```python
from cognee_community_vector_adapter_elasticsearch import register  # noqa: F401
```

Or configure it in code:

```python
import cognee
from cognee_community_vector_adapter_elasticsearch import register  # noqa: F401

cognee.config.set_vector_db_config(
    {
        "vector_db_provider": "elasticsearch",
        "vector_db_url": "http://localhost:9200",
        "vector_db_key": "",
    }
)
```

See `example.py` for a full `add -> cognify -> search` run.

## How data is laid out

Every cognee collection (`DocumentChunk_text`, `Entity_name`, ...) becomes one
index named `<prefix>-<collection>` (lowercased, e.g. `cognee-documentchunk_text`)
with this mapping:

| Field | Type | Purpose |
|-------|------|---------|
| `id` | `keyword` | cognee's data point id, returned to callers |
| `database_name` | `keyword` | dataset scope |
| `belongs_to_set` | `keyword` | node-set tags, used for filtering |
| `payload` | `object`, `enabled: false` | the full data point, stored but not indexed |
| `vector` | `dense_vector`, HNSW, cosine | the embedding |

Payloads are stored but not indexed on purpose: they are free-form and would
otherwise grow the mapping without bound.

### Dataset isolation

Datasets share indices. Each document carries a `database_name` and its
Elasticsearch `_id` is derived from `(database_name, id)`, so two datasets can
hold the same cognee entity independently while searches, deletes, and
`prune()` stay scoped to one dataset. `prune()` deletes the dataset's rows and
drops any index left empty. Use a distinct `database_name` per dataset when
constructing adapters directly; with `VECTOR_DATASET_DATABASE_HANDLER=elasticsearch`
cognee does this for you.

### Scores

Elasticsearch reports kNN hits as `(1 + cosine) / 2`. The adapter converts this
back to cosine distance (`0` identical, `2` opposite) so lower is better, as the
cognee contract requires. Result sets are capped at Elasticsearch's result
window of 10,000 rows.

## Tests

The offline unit tier runs against an in-memory fake of the client and needs no
server or keys:

```bash
uv run pytest tests/unit
```

`tests/test_elasticsearch.py` and `example.py` run the full pipeline against a
real cluster and need LLM and embedding credentials; CI runs them in
`.github/workflows/test_elasticsearch.yml`.
