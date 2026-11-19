# ChromaDB Vector Database Adapter

This is a community-contributed adapter for integrating [ChromaDB](https://www.trychroma.com/) with Cognee. It was previously bundled in cognee core and now lives here as a community package.

## About ChromaDB

ChromaDB is an open-source embedding database. This adapter talks to a ChromaDB server over its async HTTP client and stores Cognee data points as collections with cosine-distance vector search.

## Installation

```bash
pip install cognee-community-vector-adapter-chromadb
```

or from source:

```bash
cd packages/vector/chromadb
pip install .
```

## Usage

Importing the `register` module registers the adapter with cognee via
`use_vector_adapter("chromadb", ChromaDBAdapter)`:

```python
import cognee
from cognee_community_vector_adapter_chromadb import register  # noqa: F401

cognee.config.set_vector_db_config(
    {
        "vector_db_provider": "chromadb",
        "vector_db_url": "http://localhost:3002",  # your ChromaDB server
        "vector_db_key": "your-chroma-token",      # token auth credential
    }
)

await cognee.add("Your data here")
await cognee.cognify()
results = await cognee.search("search query")
```

## Configuration

| Setting | Description |
|---|---|
| `vector_db_provider` | Must be `"chromadb"`. |
| `vector_db_url` | Host/URL of the ChromaDB server (`AsyncHttpClient` host). |
| `vector_db_key` | Token used for ChromaDB token auth. |

## Dependencies

- `chromadb>=0.6,<0.7`
- `cognee>=0.5.0`

## License

Apache 2.0, same as the main Cognee project.
