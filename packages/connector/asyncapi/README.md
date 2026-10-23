# AsyncAPI Specification Connector for Cognee

[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-yellow.svg)](https://opensource.org/licenses/Apache-2.0)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Mergetober Sprint](https://img.shields.io/badge/Mergetober-Sprint%202026-orange.svg)](https://github.com/topoteretes/cognee)

A high-performance `dlt` data-source connector for [Cognee](https://github.com/topoteretes/cognee) that transforms [AsyncAPI](https://www.asyncapi.com) event-driven specifications (Kafka, WebSockets, RabbitMQ, MQTT) into persistent AI memory and interconnected knowledge graphs.

---

## Architecture Overview

Modern distributed systems and event-driven architectures rely on the Linux Foundation's AsyncAPI standard to document message brokers, event channels, and streaming payloads.

The **AsyncAPI Connector** ingests these message contracts into Cognee's persistent graph memory:

```
┌────────────────────────────────┐
│   AsyncAPI Specification       │
│ (asyncapi.yaml / asyncapi.json)│
└───────────────┬────────────────┘
                │ (Servers, Channels, Operations, Messages)
                ▼
┌────────────────────────────────┐
│    asyncapi_source() (dlt)     │
│  - Channel & Topic Parsing     │
│  - JSON Schema Payload Format  │
│  - Replace Snapshot Sync       │
└───────────────┬────────────────┘
                │ DOCUMENT_SOURCE_ATTR = "asyncapi"
                ▼
┌────────────────────────────────┐
│    Cognee Pipeline Engine      │
│    await cognee.add(source)    │
│    await cognee.cognify()      │
└───────────────┬────────────────┘
                │ Entity & Relation Extraction
                ▼
┌────────────────────────────────┐
│    Persistent Graph Memory     │ ──> AI Architecture Agents,
│  (Entities, Nodes & Vectors)   │     Kafka Copilots & Dev Bots
└────────────────────────────────┘
```

---

## Features

- **Document-Mode Ingestion:** Declares `DOCUMENT_SOURCE_ATTR = "asyncapi"`, routing event channels and schemas into Cognee's `cognify` entity-extraction pipeline.
- **Deep Channel & Message Parsing:** Discovers and formats event brokers, topics, operations (publish/subscribe), message headers, and payload schema properties (types, required fields, descriptions).
- **Full Snapshot + Forget-on-Delete:** Uses `write_disposition="replace"`. Deleting or deprecating a topic automatically prunes obsolete graph nodes via Cognee's `orphan_cleanup`.
- **Zero API Keys Required:** Runs 100% offline with isolated unit tests.

---

## Installation

```bash
# In your project or cognee-community workspace
pip install -e packages/connector/asyncapi
```

---

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_asyncapi import asyncapi_source

async def main():
    # 1. Point to your AsyncAPI specification file or directory
    source = asyncapi_source(spec_path="./asyncapi.yaml")

    # 2. Add event channels to Cognee
    print("Ingesting AsyncAPI definitions...")
    await cognee.add(source)

    # 3. Extract knowledge graph entities and relations
    print("Cognifying event architectures...")
    await cognee.cognify()

    # 4. Search and retrieve insights from the graph
    insights = await cognee.search(
        query_type="INSIGHTS",
        query_text="What Kafka topics are used for payment events and what payload fields are expected?",
    )
    print("Knowledge Graph Insights:\n", insights)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

```bash
# Run pytest test suite
pytest packages/connector/asyncapi/tests/ -v

# Run linter
ruff check packages/connector/asyncapi/
```

---

## Mergetober Hackathon

Built with ❤️ for **Mergetober** (WeMakeDevs x Hacker's Unity x Cognee).
