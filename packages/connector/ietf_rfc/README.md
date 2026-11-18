# IETF RFC & Internet Standards Connector for Cognee

The official `ietf_rfc` data-source connector for [Cognee](https://github.com/topoteretes/cognee), syncing official IETF Request for Comments (RFCs), Internet Standards, specification supersession graphs (`obsoletes` / `obsoleted_by` / `updates`), and verified technical errata directly into AI agent memory and relationship graphs.

---

## Overview

AI coding agents and security auditors frequently answer technical questions using obsolete protocol specifications—for example, citing RFC 7234 or RFC 2616 for HTTP caching rules even though RFC 9111 officially obsoleted them.

Because Cognee is fundamentally built on graph memory, the IETF standards corpus is a natural fit: `obsoletes`, `obsoleted_by`, `updates`, and `updated_by` relationships map directly onto Cognee's entity relationship graph.

Security and architecture agents can answer:
- *"Which RFC currently defines HTTP caching today, and what legacy specifications did it replace?"*
- *"Are there any verified technical errata registered for TLS 1.3 (RFC 8446)?"*
- *"Trace the supersession history of OAuth 2.0 authorization framework specifications."*

### Key Capabilities

- **Bounded Specification Scoping**: Ingests specific RFC numbers (e.g. `[7234, 9111]`), working groups (e.g. `httpbis`, `tls`, `oauth`), or local snapshot corpuses.
- **Supersession State Graphing**: Clearly records and visualizes which specifications are current and which have been obsoleted or amended.
- **Verified Errata Attachment**: Ingests official errata entries (technical/editorial corrections, affected sections, notes, and verification state).
- **Public & Rate-Limit Free**: Connects directly to the public RFC Editor and IETF Datatracker APIs with no accounts, API keys, or paid subscriptions required.
- **Document-Mode Routing**: Declares `DOCUMENT_SOURCE_ATTR = "ietf_rfc"` for automatic relationship linking in Cognee's `cognify()` pipeline.
- **Reconciliation & Orphan Cleanup**: Employs `write_disposition="replace"` so that metadata updates (new errata, status changes) cleanly refresh existing knowledge.

---

## Installation

```bash
pip install "cognee-community-connector-ietf-rfc"
```

Or from the `cognee-community` monorepo:

```bash
cd packages/connector/ietf_rfc
pip install -e .
```

---

## Quickstart

### 1. Ingest RFC Standards into Cognee

```python
import asyncio
import cognee
from cognee_community_connector_ietf_rfc import ietf_rfc_source


async def main():
    # 1. Initialize connector with HTTP Semantics and Caching standards family
    source = ietf_rfc_source(
        rfc_numbers=[
            9110,  # HTTP Semantics (current)
            9111,  # HTTP Caching (current)
            7234,  # Legacy HTTP/1.1 Caching (obsoleted)
        ]
    )

    # 2. Ingest into Cognee staging
    await cognee.add(source)

    # 3. Cognify: extract entities, relationships, and vector embeddings
    await cognee.cognify()

    # 4. Query agent memory for authoritative protocol guidance
    results = await cognee.search(
        "Which RFC defines HTTP caching mechanisms today and what did it replace?"
    )
    for res in results:
        print(res)


if __name__ == "__main__":
    asyncio.run(main())
```

---

### 2. Offline / Local Snapshot Ingestion

```python
source = ietf_rfc_source(
    snapshot_data="./rfc_standards_snapshot.json",
)
```

---

## Document Schema & Metadata

Each RFC specification is formatted as a structured Markdown document:

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Canonical persistent identifier (e.g. `rfc9111`) |
| `number` | `int` | Integer RFC number |
| `title` | `str` | Standardized header `[IETF RFC <Number>] <Title>` |
| `url` | `str` | Official RFC Editor URL |
| `status` | `str` | Maturity level (`Internet Standard`, `Proposed Standard`, etc.) |
| `is_obsoleted` | `bool` | True if the specification has been superseded |
| `obsoletes` | `list[int]` | RFC numbers this document obsoleted |
| `obsoleted_by` | `list[int]` | RFC numbers that have obsoleted this document |
| `cognee_node_set` | `list[str]` | Scoped tags (`ietf`, `rfc:<num>`, `status:<status>`, `standards`) |
| `content` | `str` | High-signal Markdown body with supersession badges, graph, and errata |

---

## Running Tests

Run the hermetic test suite:

```bash
PYTHONPATH=packages/connector/ietf_rfc pytest packages/connector/ietf_rfc/tests/ -v
```
