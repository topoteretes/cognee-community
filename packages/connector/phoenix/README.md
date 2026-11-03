# Arize Phoenix Connector for Cognee

A high-performance data-source connector for [Cognee](https://github.com/topoteretes/cognee) that ingests LLM execution traces, OpenInference spans (LLM calls, tool executions, retriever spans), and evaluation annotations from self-hosted or cloud [Arize Phoenix](https://phoenix.arize.com/) into Cognee's persistent spatial knowledge graph and vector memory.

Resolves **[cognee#6450](https://github.com/topoteretes/cognee/issues/6450)**.

---

## Capabilities

- **Trace & Span Ingestion**: Maps root inputs, completions, execution duration, and full span trees into readable Markdown trace documents.
- **OpenInference Support**: Structured extraction of model names, token counts (`prompt`, `completion`, `total`), tool invocations with arguments/results, retriever document citations, and caught exceptions.
- **Node-Set Graph Links**: Automatically builds node-set filters for `project`, `session`, `trace`, `model_name`, and `tool_name` so agents can traverse execution paths directly in the Cognee graph (e.g. `session → trace → tool`).
- **Evaluation Annotations**: Links human and LLM-assisted evaluations (`label`, `score`, `explanation`) directly to the trace document.
- **Incremental Cursor Sync**: Insertion-order cursor pagination ensures only new or updated spans and traces are fetched on subsequent pipeline runs.
- **Production Resilience**: Exponential backoff retry on HTTP 429 rate limits, and hermetic error handling for HTTP 401 and 404 responses.

---

## Installation

```bash
pip install cognee-community-connector-phoenix
```

---

## Configuration

Set the following environment variables (or pass them directly to `PhoenixClient` / `phoenix_data`):

| Variable | Description | Default |
| :--- | :--- | :--- |
| `PHOENIX_COLLECTOR_ENDPOINT` | URL of the Phoenix instance (e.g. `http://localhost:6006` or `https://app.phoenix.arize.com`) | `http://localhost:6006` |
| `PHOENIX_API_KEY` | Optional API key for Arize Phoenix Cloud / protected instances | None |
| `PHOENIX_PROJECT_NAME` | Target Phoenix project to sync | `default` |

---

## Quickstart

```python
import cognee
from cognee_community_connector_phoenix import phoenix_data

# Yield structured trace documents and add to Cognee memory
for document in phoenix_data(
    base_url="http://localhost:6006",
    project_name="agent-production-logs",
    limit=50
):
    print(f"Ingesting trace: {document['id']}")
    # cognee.add(document["text"], dataset_name="phoenix_traces")

# Run cognee cognify to build spatial knowledge graph connections
# cognee.cognify(dataset_name="phoenix_traces")
```

---

## Unit Testing

Run the isolated hermetic test suite:

```bash
PYTHONPATH=. pytest tests/ -v
```
