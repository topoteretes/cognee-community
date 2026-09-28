# Infino vector adapter for cognee

[Infino](https://github.com/infino-ai/infino) is an open-source embedded
retrieval engine (Apache-2.0): BM25, vector, and hybrid search plus SQL over
data stored as standard Apache Parquet, on a local path or directly on object
storage. There is no server to run — the `infino` package is the engine, and
the `vector_db_url` names where your memory lives.

What that means for cognee:

- **Nothing to deploy.** `pip install` and point the URL at a directory. No
  container, no service, no API key for the store.
- **Memory as durable files.** Point the same URL at an `s3://` prefix and the
  agent's memory persists on object storage as spec-compliant Parquet — you
  can read the same files back with pandas, DuckDB, or pyarrow.
- **Offline tests against the real engine.** Because the engine is embedded,
  this package's whole test tier (contract + behavior) runs with no network
  and no secrets.

## Installation

```bash
pip install cognee-community-vector-adapter-infino
```

## Usage

```python
import cognee
from cognee import config

# Register the adapter with cognee.
from cognee_community_vector_adapter_infino import register  # noqa: F401

config.set_vector_db_config({
    "vector_db_provider": "infino",
    "vector_db_url": "./infino_storage",      # or "s3://your-bucket/memory"
    "vector_db_key": "",
    "vector_dataset_database_handler": "infino",
})
```

See `example.py` for a complete remember/recall run, and `.env.example` for
the environment variables.

## Configuration

| Variable | Meaning |
| --- | --- |
| `VECTOR_DB_PROVIDER` | `infino` |
| `VECTOR_DB_URL` | Where the data lives: a local directory or an `s3://` prefix. Object-store credentials come from the standard AWS environment. |
| `VECTOR_DATASET_DATABASE_HANDLER` | `infino` (per-dataset catalogs under the cognee system root) |

## Notes

- Embeddings must be at least 16-dimensional (an engine constraint; every
  real embedding model clears it).
- Vector scores are cosine distances, lower is better, matching cognee's
  `ScoredResult` contract.
- Infino also supports BM25 and fused hybrid search over the same rows, so
  keyword-exact recall (identifiers, dates, names) is available on the same
  table — see the Infino docs at https://infino.ai/docs.

## Running the tests

```bash
poetry install
poetry run pytest tests/unit -q
```

No services or keys needed: the tests run the real engine against a temporary
directory.
