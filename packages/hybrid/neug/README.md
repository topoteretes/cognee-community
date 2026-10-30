# Cognee Community NeuG Hybrid Adapter

Community-maintained adapter that lets Cognee use [NeuG](https://github.com/alibaba/neug)
as a **hybrid** backend: one embedded database file serves both the knowledge
graph (Cypher) and the vector store (HNSW ANN), with no separate server to run.

NeuG is an embedded graph + vector engine, so this adapter is a good fit for
local / edge AI-memory deployments where you want graph and vector in a single
zero-network store.

## Installation

```bash
pip install cognee-community-hybrid-adapter-neug
```

The `neug` engine is a required dependency of this package and is installed
automatically. It hard-pins `protobuf==5.29.6`; that pin is scoped to the
environment where you install this adapter and does not affect cognee core's
own dependency set.

## Usage

```python
import asyncio
import os
import pathlib
from os import path

# Importing the register module is what makes the "neug" provider name valid.
# Do it before cognee builds any engine.
from cognee_community_hybrid_adapter_neug import register  # noqa: F401

from cognee import SearchType, add, cognify, config, prune, search


async def main():
    # NeuG is single-tenant (graph + vector share one embedded file), so it has
    # no per-dataset isolation. Access control must be turned off.
    os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] = "false"

    system_path = pathlib.Path(__file__).parent
    config.system_root_directory(path.join(system_path, ".cognee_system"))
    config.data_root_directory(path.join(system_path, ".cognee_data"))

    # Optional: pin the embedded database file. Defaults to
    # <cognee data root>/databases/neug_db.
    # os.environ["NEUG_DB_PATH"] = "/tmp/my_neug_db"

    config.set_relational_db_config({"db_provider": "sqlite"})

    # Register NeuG as BOTH the graph and the vector provider (same name).
    config.set_graph_db_config({"graph_database_provider": "neug"})
    config.set_vector_db_config({"vector_db_provider": "neug"})

    await prune.prune_data()
    await prune.prune_system(metadata=True)

    await add(
        """
        Natural language processing (NLP) is an interdisciplinary
        subfield of computer science and information retrieval.
        """
    )

    await cognify()

    results = await search(query_type=SearchType.GRAPH_COMPLETION, query_text="Tell me about NLP")
    for result in results:
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

**Graph database:**
- `graph_database_provider`: set to `"neug"`

**Vector database (enables hybrid mode — set both to `"neug"`):**
- `vector_db_provider`: set to `"neug"`

Connection parameters (url/port/username/password) are accepted for factory
compatibility but unused: the database is an embedded file, not a server.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ENABLE_BACKEND_ACCESS_CONTROL` | `true` | **Must be `false` for NeuG** (single-tenant; no dataset handler). |
| `NEUG_DB_PATH` | `<data root>/databases/neug_db` | Path of the embedded NeuG database file. |

## Single-tenancy caveat

NeuG stores graph and vector data in **one** embedded database file shared
through a process-level connection manager. There is no per-user / per-dataset
isolation, so this package does **not** register a dataset-database handler.

With `ENABLE_BACKEND_ACCESS_CONTROL=true` (cognee's default) core requires a
handler for every active backend and will raise `EnvironmentError` for NeuG.
Set `ENABLE_BACKEND_ACCESS_CONTROL=false` to run NeuG single-tenant.

## Known limitations

- **Lexical search (`CHUNKS_LEXICAL`)**: cognee routes this search type through
  its in-memory `BM25ChunksRetriever`. NeuG's native full-text (bm25) index is
  created inside the database file, but the community registration path has no
  hook to route `CHUNKS_LEXICAL` to it, so native FTS is **not** wired up here.
  Vector, graph and hybrid retrieval are unaffected.
- **NeuG Cypher dialect**: the adapter works around several NeuG 0.2.0 engine
  limitations internally (no `keys()`; `SKIP`/`LIMIT` parameter and offset
  handling; single-argument `properties()`; RETURN map literals). These are
  transparent to callers but constrain raw Cypher pass-through.

## Requirements

- Python >= 3.11
- neug >= 0.2.0

## About NeuG

NeuG is an embedded graph database from the GraphScope team that unifies
property-graph (Cypher), vector (HNSW / exact ANN) and full-text indexing in a
single C++ engine with zero network overhead. This adapter ports cognee's
single-table graph schema onto NeuG and stores vector collections as node
tables in the same file.
