# Cognee Qdrant Adapter

## Installation

If published, the package can be simply installed via pip:

```bash
pip install cognee-community-vector-adapter-qdrant
```

In case it is not published yet, you can use poetry to locally build the adapter package:

```bash
pip install poetry
poetry install # run this command in the directory containing the pyproject.toml file
```

## Connection Setup

For a quick local setup, you can run a docker container that qdrant provides (https://qdrant.tech/documentation/quickstart/). 
After this, you will be able to connect to the Qdrant DB through the appropriate ports. The command for running the docker 
container looks something like the following:

```
docker run -p 6333:6333 -p 6334:6334 \
    -v "$(pwd)/qdrant_storage:/qdrant/storage:z" \
    qdrant/qdrant
```

## Usage

Import and register the adapter in your code:
```python
from cognee_community_vector_adapter_qdrant import register
```

Also, specify the dataset handler in the .env file:
```dotenv
VECTOR_DATASET_DATABASE_HANDLER="qdrant"
```

## Quantization

The adapter supports Qdrant's quantization methods, including TurboQuant (introduced in Qdrant 1.18). Quantization compresses stored vectors and reduces memory cost; it is disabled by default.

| Mode  | Compression | Notes                                                  |
|-------|-------------|--------------------------------------------------------|
| `tq4` | 8x          | TurboQuant 4-bit. Recommended default; SQ-level recall |
| `tq2` | 16x         | TurboQuant 2-bit. Beats binary 2-bit by 9–24 pp        |
| `tq1.5` | ~21x      | TurboQuant 1.5-bit                                     |
| `tq1` | 32x         | TurboQuant 1-bit. Beats binary 1-bit by 9–21 pp        |
| `sq`  | 4x          | Scalar int8 — safe baseline                            |
| `bq1` | 32x         | Binary 1-bit                                           |
| `bq2` | 16x         | Binary 2-bit                                           |
| `pq`  | 16x         | Product quantization (compression ratio X16)           |
| `none` | 1x         | No quantization (default, backward-compatible)         |

Set the mode via env var:

```dotenv
QDRANT_QUANTIZATION=tq4
QDRANT_QUANTIZATION_ALWAYS_RAM=true
QDRANT_QUANTIZATION_RESCORE=true
QDRANT_QUANTIZATION_OVERSAMPLING=2.0
```

Requires `qdrant-client>=1.18` and Qdrant server `>=1.18` for any `tq*` mode.

### Migrating existing collections

Setting `QDRANT_QUANTIZATION` only affects newly created collections. To enable quantization on an existing collection without re-ingesting data, call `update_quantization`:

```python
from cognee.infrastructure.databases.vector import get_vector_engine

adapter = get_vector_engine()
await adapter.update_quantization("Entity_name")
await adapter.update_quantization("DocumentChunk_text")
```

Qdrant rebuilds the affected index in the background; queries during the rebuild transparently fall back to full vectors.

## Example
See example in `example.py` file.
