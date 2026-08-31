# Cognee Pinecone Adapter

## Installation

If published, the package can be simply installed via pip:

```bash
pip install cognee-community-vector-adapter-pinecone
```

In case it is not published yet, you can use poetry to locally build the adapter package:

```bash
pip install poetry
poetry install # run this command in the directory containing the pyproject.toml file
```

## Connection Setup

To use the Pinecone adapter, you need to:

1. Sign up for a Pinecone account at https://www.pinecone.io/
2. Create a new project and get your API key
3. Note your environment details (cloud provider and region)

## Usage

Register the adapter with cognee before configuring `vector_db_provider="pinecone"`.
Importing `register` already registers the adapter as a side effect, and `register()`
is also callable directly (both are idempotent):

```python
from cognee_community_vector_adapter_pinecone import register

register()
```

## Example

See example in `example.py` file.
