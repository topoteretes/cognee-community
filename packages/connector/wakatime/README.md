# WakaTime Connector for Cognee

A `dlt` data-source connector that ingests WakaTime developer coding activity, time metrics, active projects, languages, and editors into Cognee memory.

Part of the **Cognee Mergetober Hackathon** (`#6218`).

---

## Features

- **Daily Summaries Ingestion:** Ingest granular daily developer time logs, breakdown by project, programming language, branch, and text editor.
- **Productivity Overviews:** Pull aggregated stats (7-day, 30-day, or all-time) capturing top coding languages, primary projects, and daily averages.
- **Full Snapshot & Forget-on-Delete:** Uses `write_disposition="replace"` so metrics cleanly refresh on every run.
- **Hermetic & Authenticated:** Supports API keys and custom HTTP clients for secure private tracking or offline testing.
- **Cognee Document Mode:** Tags resources with `DOCUMENT_SOURCE_ATTR` to route directly through Cognee's `cognify` entity-extraction pipeline.

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_wakatime import wakatime_source

async def main():
    # 1. Initialize WakaTime source with your API key
    source = wakatime_source(
        api_key=os.getenv("WAKATIME_API_KEY"),
        days_back=7,
        include_overview=True,
    )

    # 2. Ingest into Cognee
    await cognee.add(source)

    # 3. Cognify into knowledge graph
    await cognee.cognify()

    # 4. Search productivity insights
    results = await cognee.search("Which project did I spend the most time coding on this week?")
    for res in results:
        print("-", res)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Tests

Run the hermetic test suite:

```bash
pytest packages/connector/wakatime/tests
```
