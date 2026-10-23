# Jupyter Notebooks (`.ipynb`) Connector for Cognee

A production-grade data-source connector for [Cognee](https://github.com/topoteretes/cognee) that statically ingests Jupyter Notebooks (`.ipynb`) into Cognee's GraphRAG engine with full-snapshot synchronization and automated orphan pruning (*forget-on-delete*).

---

## 🌟 Key Features

- **Zero Code Execution (OWASP LLM02):** Statically parses native `nbformat` v4 and v3 JSON files using Python's standard library. Never starts an ipykernel or invokes `exec()`/`eval()`.
- **Clean Content Segregation:** Formats markdown cells into structured prose narrative, wraps code cells into language-tagged fenced blocks, and extracts text outputs (`stdout`, `text/plain`, runtime tracebacks).
- **Noise Suppression:** Automatically discards large base64-encoded image outputs (plots, charts) and strips ANSI color escape codes from exception traces.
- **Pre-Ingestion Secret Redaction:** Scrubs Bearer tokens, API keys, AWS keys (`AKIA...`), GitHub PATs (`ghp_...`), and OpenAI keys (`sk-...`) before graph emission.
- **Forget-on-Delete:** Configured with `write_disposition="replace"` and `DOCUMENT_SOURCE_ATTR = "jupyter"`. Removed or renamed notebooks drop out of the snapshot, allowing Cognee's native `orphan_cleanup` to prune stale nodes and embeddings automatically.

---

## 📦 Installation

```bash
pip install cognee-community-connector-jupyter
```

Or install in development mode from `cognee-community`:

```bash
cd packages/connector/jupyter
pip install -e .
```

---

## 🚀 Quickstart

```python
import asyncio
from pathlib import Path
import cognee
from cognee_community_connector_jupyter import jupyter_source


async def main():
    # 1. Point to your notebooks directory or specific list of files
    source = jupyter_source(
        workspace_dir="./notebooks",
        include_outputs=True,  # Ingest text outputs and stdout
        max_output_chars=2000,  # Prevent output explosion
        redact_secrets=True,  # Scrub credentials and tokens
    )

    # 2. Add to Cognee memory and build the graph
    await cognee.add(source)
    await cognee.cognify()

    # 3. Query the notebook knowledge graph
    results = await cognee.search("What were the key findings in customer churn analysis?")
    for result in results:
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## 🧪 Running Offline Tests

The connector includes an offline test suite with zero live network or runtime dependencies:

```bash
cd packages/connector/jupyter
python run_tests.py
```

All 27 test cases validate parsing, output rendering, secret scrubbing, and file discovery.
