# Cognee Community Connector: Helicone

Community data-source connector for [Helicone](https://www.helicone.ai/) LLM observability, versioned prompt templates, curated evaluation datasets, and user feedback traces into [Cognee](https://github.com/topoteretes/cognee).

## Overview
Helicone is a leading open-source LLM observability platform. Teams using Helicone store versioned prompt templates, evaluation suites, automated judges, and user feedback ratings.

Integrating Helicone with Cognee allows AI agents to answer critical questions about AI systems (e.g. *Which prompt version is currently deployed in production?*, *Which test cases failed the hallucination evaluator?*, *What negative feedback did users report this week?*).

## Features
- **Prompt Template Versioning:** Ingests prompt templates, versions, model configurations, and timestamps.
- **Evaluation Datasets:** Ingests benchmark datasets, input cases, and expected outputs.
- **Evaluator Quality Definitions:** Captures automated judge criteria and scoring logic.
- **Curated Feedback Traces:** Extracts user-rated feedback traces (thumbs up/down, user comments) with strict bounds to prevent raw proxy log flooding.
- **100% Hermetic Testing:** Tested offline with zero live cloud dependencies.

## Installation
```bash
pip install cognee-community-connector-helicone
```

## Quickstart
```python
import asyncio
from cognee_community_connector_helicone import HeliconeConnector
import cognee

async def main():
    connector = HeliconeConnector(
        api_key="your_helicone_api_key",
        base_url="https://api.helicone.ai",
        fetch_prompts_flag=True,
        fetch_datasets_flag=True,
        fetch_evaluators_flag=True,
        fetch_feedback_traces_flag=True,
    )

    data = await connector.fetch_all()
    print(f"Retrieved {len(data['prompts'])} prompts and {len(data['datasets'])} datasets.")

    # Ingest into Cognee Knowledge Graph
    for p in data["prompts"]:
        await cognee.add(p["clean_text"])
    
    for d in data["datasets"]:
        await cognee.add(d["clean_text"])

    await cognee.cognify()
    print("Ingestion complete!")

if __name__ == "__main__":
    asyncio.run(main())
```
