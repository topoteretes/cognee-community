# Braintrust AI Evaluation Platform Connector for Cognee

A `dlt` data-source connector that ingests AI evaluation experiments, benchmark scores, evaluation datasets, and prompt engineering templates from Braintrust into Cognee memory for automated LLMOps and RAG benchmarking intelligence.

Part of the **Cognee Mergetober Hackathon** (`#6303`).

---

## Features

- **Project Inventory:** Ingests Braintrust projects, descriptions, and dashboard URLs.
- **Experiment Tracking:** Ingests evaluation experiment runs, benchmark scores (factuality, relevance, latency), and model parameters.
- **Dataset Cataloging:** Ingests evaluation test suites and golden dataset metadata.
- **Prompt Registry:** Ingests registered prompt templates, message sequences, and target models.
- **Snapshot Replacement:** Configured with `write_disposition="replace"` for complete synchronization.
- **Document Mode:** Tags resources with `DOCUMENT_SOURCE_ATTR` to route directly through Cognee's `cognify` entity-extraction pipeline.

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_braintrust import braintrust_source

async def main():
    # 1. Initialize Braintrust connector
    source = braintrust_source(
        api_key=os.getenv("BRAINTRUST_API_KEY"),
        project_names=["rag-eval-prod"],
        include_prompts=True,
        include_datasets=True,
    )

    # 2. Add to Cognee memory
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    await cognee.cognify()

    # 4. Search LLMOps intelligence
    results = await cognee.search("Which experiments had the highest benchmark scores?")
    for res in results:
        print("-", res)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the hermetic offline test suite:

```bash
pytest packages/connector/braintrust/tests
```
