# Drizzle ORM Schema Connector for Cognee

A dedicated [Cognee](https://github.com/topoteretes/cognee) data-source connector for **Drizzle ORM** schemas (`schema.ts`). It parses TypeScript database definitions into structured, entity-linked Markdown documents optimized for Cognee's `cognify()` knowledge graph pipeline.

---

## 🎯 The Problem

AI coding agents and autonomous developer assistants (such as Claude Code, Cursor, Codex, OpenCode, and Copilot) frequently hallucinate database structures, column names, nullability constraints, and relational mappings when generating SQL queries or backend application code.

In modern TypeScript projects, [Drizzle ORM](https://orm.drizzle.team) acts as the single source of truth for database architecture. However, standard plain-text file chunking splits table definitions, column types, and separate `relations()` declarations across unrelated chunks, breaking the semantic foreign key links that AI agents need to reason about relational joins.

---

## 🚀 How It Works

The Drizzle connector parses your project's `schema.ts` (or schema directory) and normalizes tables, columns, enums, and relations into coherent Markdown entities:

1. **Dialect Support**: Automatically detects and extracts tables across:
   - PostgreSQL (`pgTable`, `pgEnum`)
   - MySQL (`mysqlTable`, `mysqlEnum`)
   - SQLite (`sqliteTable`)
2. **Column Details**: Preserves types (`serial`, `varchar`, `text`, `integer`, `timestamp`, `uuid`), primary keys, nullability, unique constraints, and foreign key references (`references(() => ...)`).
3. **Relational Mappings**: Extracts explicit Drizzle `relations()` declarations (`one()` and `many()`), linking parent and child tables in the knowledge graph.
4. **Document Mode (`DOCUMENT_SOURCE_ATTR`)**: Tags the dlt source with `DOCUMENT_SOURCE_ATTR = "drizzle"`, routing documents through Cognee's `cognify()` LLM entity-extraction pipeline.
5. **Full Snapshot & Forget-on-Delete**: Employs `@dlt.resource(write_disposition="replace")` with stable deterministic IDs (`drizzle:table:{name}` and `drizzle:enum:{name}`). When schemas evolve or tables are removed, Cognee's automated orphan cleanup clears deprecated models from memory.

---

## 📦 Installation

```bash
# In your Cognee environment:
pip install "cognee-community-connector-drizzle"
# or with uv:
uv add cognee-community-connector-drizzle
```

---

## 💻 Quickstart

### Ingesting into Cognee Memory

```python
import asyncio
from pathlib import Path
import cognee
from cognee_community_connector_drizzle import drizzle_source

DATASET_NAME = "database_schema"


async def main():
    # 1. Point to your schema file or directory
    source = drizzle_source(schema_path="src/db/schema.ts")

    # 2. Add to Cognee memory (full snapshot)
    await cognee.remember(source, dataset_name=DATASET_NAME)

    # 3. Cognify into knowledge graph
    await cognee.cognify(datasets=[DATASET_NAME])

    # 4. Ask questions directly from agent memory
    results = await cognee.search(
        "What tables link to the users table and what are the relations?", dataset_name=DATASET_NAME
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## 🧪 Testing

The connector includes hermetic unit tests that run entirely offline without requiring live database connections or network access:

```bash
cd packages/connector/drizzle
pytest
```

---

## 📄 License

Apache 2.0. Built for the open-source community as part of the **Mergetober 2026** Hackathon.
