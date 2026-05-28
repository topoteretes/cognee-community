# openGauss Vector Database Adapter

This is a community-contributed adapter for integrating openGauss DataVec with Cognee.

## About openGauss

openGauss is an open-source relational database with ACID transactions and built-in vector capabilities via DataVec (6.0.0+). It provides native `VECTOR(n)` type, HNSW / IVFFLAT / IVFPQ indexes, and hybrid SQL + vector similarity search — all in a single database.

## Installation

1. Install the required dependencies:
   ```bash
   # Option 1: Install dependencies directly
   pip install psycopg2-binary>=2.9.9

   # Option 2: Install as a package (if published)
   pip install cognee-community-vector-adapter-opengauss

   # Option 3: Install from source
   cd packages/vector/opengauss
   pip install .
   ```

2. Import and register the adapter in your code:
   ```python
   from cognee_community_vector_adapter_opengauss import register
   ```

## Setup

Start an openGauss container:

```bash
docker run -d \
    --name opengauss \
    --privileged=true \
    --restart=always \
    -p 5432:5432 \
    -e GS_PASSWORD=openGauss@123 \
    -v ./opengauss:/var/lib/opengauss/data \
    opengauss/opengauss:7.0.0-RC1
```

No additional extension required — DataVec is built in.

## Configuration

Create a `.env` file from the template:

```bash
cp .env.example .env
# Edit .env with your LLM / embedding / database settings
```

Configure Cognee to use openGauss:

```python
from dotenv import load_dotenv
from cognee import config
from cognee_community_vector_adapter_opengauss import register

load_dotenv()

config.set_vector_db_config({
    "vector_db_provider": "opengauss",
    "vector_db_url": "postgresql://gaussdb:openGauss%40123@localhost:5432/postgres",
})
```

Required environment variables:

```dotenv
OPENGAUSS_URL=postgresql://gaussdb:OpenGauss%40123@localhost:5432/postgres
ENABLE_BACKEND_ACCESS_CONTROL=false
```

LLM and embedding providers must also be configured — see the [Cognee docs](https://docs.cognee.ai).

Optional openGauss settings (all have defaults):

```dotenv
OPENGAUSS_SCHEMA_NAME=cognee           # Schema for vector tables
OPENGAUSS_INDEX_TYPE=HNSW              # HNSW | IVFFLAT | IVFPQ | HNSW-PQ
OPENGAUSS_DISTANCE_STRATEGY=COSINE     # COSINE | EUCLIDEAN | MANHATTAN | INNER_PROD
OPENGAUSS_CREATE_INDEX=false           # Enable when index implementation is stable
```

## Usage Example

```python
import asyncio
import os
import pathlib
from dotenv import load_dotenv
from cognee import add, cognify, config, search, SearchType
from cognee_community_vector_adapter_opengauss import register

load_dotenv()

async def main():
    # Set working directories
    root = pathlib.Path(__file__).parent
    config.system_root_directory(str(root / ".cognee_system"))
    config.data_root_directory(str(root / ".cognee_data"))

    # Configure openGauss
    config.set_vector_db_config({
        "vector_db_provider": "opengauss",
        "vector_db_url": os.getenv(
            "OPENGAUSS_URL",
            "postgresql://gaussdb:openGauss%40123@localhost:5432/postgres",
        ),
    })

    # Add and process data
    await add("Natural language processing is a subfield of AI.")
    await cognify()

    # Search
    results = await search("Tell me about NLP", query_type=SearchType.GRAPH_COMPLETION)
    print(results)

asyncio.run(main())
```

## Features

- **ACID transactions**: Full transactional guarantees for vector writes
- **Multiple index types**: HNSW, IVFFLAT, IVFPQ, and HNSW-PQ index algorithms
- **Hybrid search**: Combine SQL filtering with vector similarity search in a single query
- **Schema isolation**: All vector tables live in a configurable `cognee` schema
- **Self-contained**: No separate vector database or extension required — DataVec is built in

## Testing

Run the tests to verify the adapter works correctly:

```bash
python tests/test_opengauss.py
```

## Dependencies

- `psycopg2-binary>=2.9.9`: PostgreSQL database adapter for Python

## Production Deployment

Deploy openGauss on bare metal or Kubernetes for high availability. It supports active-standby replication and automatic failover. See the [openGauss installation guide](https://docs.opengauss.org/) for details.

## Support

For issues specific to this adapter:
1. Check the [openGauss documentation](https://docs.opengauss.org/)
2. Create an issue in the main Cognee repository with the "community-adapter" label
3. Refer to the `examples/` and `tests/` directories for usage patterns

## License

This adapter is licensed under the MIT license, same as the main Cognee project.
