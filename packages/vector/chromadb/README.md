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

This adapter supports single-tenant deployments. Set
`ENABLE_BACKEND_ACCESS_CONTROL=false` before starting Cognee; this adapter does
not register a dataset database handler.

```python
import cognee
from cognee_community_vector_adapter_chromadb import register  # noqa: F401

cognee.config.set_vector_db_config(
    {
        "vector_db_provider": "chromadb",
        "vector_db_url": "http://localhost:3002",  # your ChromaDB server
        "vector_db_key": "your-chroma-token",  # token auth credential
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
| `vector_db_name` | Existing Chroma database to use; an empty value uses `default_database`. |

Include the server port in `vector_db_url`, as in the example above.

## Dependencies

- `chromadb>=0.6,<0.7`
- `cognee==1.5.4`

## Tests

From the repository root, install the package and run its offline test tiers:

```bash
pip install -e packages/vector/chromadb pytest pytest-asyncio
pytest packages/vector/chromadb/tests/unit packages/vector/chromadb/tests/integration -q
```

The integration tests use a real Chroma server in process, temporary storage,
HTTPX's ASGI transport, and deterministic embeddings. They require no listening
server or LLM credentials. The original `tests/test_chromadb.py` pipeline example
requires a separately running Chroma server and LLM/embedding configuration.

## License

Apache 2.0, same as the main Cognee project.
