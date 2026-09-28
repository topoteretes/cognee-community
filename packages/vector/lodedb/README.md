# Cognee LodeDB Adapter

[LodeDB](https://github.com/Egoist-Machines/LodeDB) is a local-first, on-disk, in-process
vector database: no server, no account, no API key, and your data never leaves the machine.
It has an exact brute-force scan by default (results don't drift with index size), optional
IVF-style ANN for large corpora, hybrid (vector + BM25) retrieval, and O(changed) delta
persistence for fast incremental writes.

This package registers LodeDB as a cognee `vector_db_provider`.

## Installation

If published, install via pip:

```bash
pip install cognee-community-vector-adapter-lodedb
```

Otherwise build it locally from this directory:

```bash
pip install poetry
poetry install   # run in the directory containing pyproject.toml
```

The adapter itself ships in the `lodedb` package
(`lodedb.local.integrations.cognee.CogneeLodeDBAdapter`); this package registers it with
cognee. It needs LodeDB >= 1.3.1 (the release that added the cognee adapter).

## Usage

Import and register the adapter, then point cognee at a local directory:

```python
import cognee
from cognee_community_vector_adapter_lodedb import register  # noqa: F401

cognee.config.set_vector_db_config(
    {
        "vector_db_provider": "lodedb",
        "vector_db_url": "./.lodedb",  # base directory for LodeDB indexes
    }
)
```

Or configure it entirely from the environment (see `.env.example`):

```dotenv
VECTOR_DB_PROVIDER=lodedb
VECTOR_DB_URL=./.lodedb
```

`vector_db_url` is a directory (created if absent). LodeDB keeps one index per cognee
collection under it. `vector_db_key` is unused (LodeDB is local and needs no credential).

## How it works

cognee owns embedding via its configured `EmbeddingEngine`, so the adapter uses LodeDB's
vector-in path. For each cognee `DataPoint` it embeds the indexed field and stores the vector
in LodeDB; the serialized DataPoint payload is kept in LodeDB's raw-text sidecar (so
`retrieve` and `include_payload` searches return it), and `belongs_to_set` membership is stored
as scalar metadata presence keys so cognee's `node_name` (NodeSet) filtering pushes into
LodeDB's metadata planner. cognee ranks by cosine distance (lower is better), so search results
report `1 - cosine_similarity`.

The embedding dimension must be a positive multiple of 8 (a LodeDB vector-index requirement).
Common embedding dimensions (384, 768, 1024, 1536, 3072) all satisfy this.

## Example

See `example.py`.
