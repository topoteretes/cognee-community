# Cognee LeetCode Community Connector

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A community connector for [Cognee](https://github.com/topoteretes/cognee) that ingests **LeetCode** algorithmic problems, topic tags, difficulty ratings, and solved submission histories into Cognee's AI Memory and Developer Knowledge Graph.

## Features

- **Public & Authenticated Ingestion:** Ingest any user's public accepted submissions without requiring credentials, or provide `LEETCODE_SESSION` cookie for private accounts.
- **Topic Tag & Difficulty Correlation:** Automatically maps algorithmic paradigms (Dynamic Programming, Graph Theory, Binary Search, Trees) to individual problem nodes.
- **Developer Memory & Prep AI:** Enables AI agents to reason about a developer's algorithmic strengths, interview prep coverage, and solved problem history.
- **Deterministic Entity Hashing:** Uses deterministic SHA-256 ID generation preventing duplicate nodes during continuous re-syncs.

## Installation

```bash
pip install cognee-community-connector-leetcode
```

Or install from source:

```bash
git clone https://github.com/bhomvratrai-glitch/cognee-community.git
cd cognee-community/packages/connector/leetcode
pip install -e .
```

## Quickstart

```python
from cognee_community_connector_leetcode import LeetCodeConnector

# Initialize connector for any public LeetCode handle
connector = LeetCodeConnector(username="your_leetcode_handle")

# Fetch recent solved problem documents for Cognee
documents = connector.fetch_documents(limit=25)
print(f"Retrieved {len(documents)} algorithmic documents for Cognee ingestion.")
```

## Running Tests

```bash
pytest tests/
```
