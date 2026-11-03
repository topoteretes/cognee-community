# Feishu / Lark Docs & Wiki Connector for Cognee

A production-ready data-source connector for **[Feishu](https://open.feishu.cn/)** and **[Lark](https://open.larksuite.com/)** documentation and wiki knowledge spaces.

Syncs team specifications, technical proposals, meeting minutes, and knowledge spaces directly into Cognee's semantic memory graph.

## Features

- **Rich Document Block Parser**: Converts Feishu Docx block trees (H1–H6, rich text, bold/italic/inline-code, code blocks, bullet/ordered lists, quotes, todos, callouts) into clean semantic markdown.
- **Wiki Space & Hierarchy Navigation**: Traverses wiki spaces (`/open-apis/wiki/v2/spaces/:space_id/nodes`) and extracts linked Docx documents.
- **Graph Grouping without LLM Processing**: Generates `cognee_node_set` entries for documents (`feishu:doc:<id>`) and wiki spaces (`feishu:space:<id>`).
- **Dual Cloud Domain Support**: Seamlessly toggles between Feishu China (`open.feishu.cn`) and Lark Global (`open.larksuite.com`).
- **Offline Snapshot & Live API Execution**: Hermetic test support via offline snapshots and live API access via tenant access tokens.
- **DLT Full-Snapshot Replace Sync**: Guarantees deterministic, content-hashed syncs (`DOCUMENT_SOURCE_ATTR = "feishu"`).

## Installation

```bash
pip install cognee-community-connector-feishu
```

## Quick Start

### 1. Ingest via Cognee Python API

```python
import cognee
from cognee_community_connector_feishu import feishu_source

# Option A: Live Feishu/Lark app credentials
source = feishu_source(
    app_id="cli_a1b2c3d4e5",
    app_secret="your_app_secret_here",
    space_ids=["space_rnd_asia"],
    domain="feishu",  # or "lark"
)

# Option B: Offline JSON snapshot file
source = feishu_source(snapshot_data="/path/to/feishu_snapshot.json")

# Ingest into Cognee
cognee.add(source)
cognee.cognify()
```

### 2. Required App Scopes in Feishu / Lark Developer Console

- `docx:document:readonly`
- `wiki:wiki:readonly`

## Testing

Run unit tests hermetically:

```bash
pytest packages/connector/feishu/tests/ -v
```
