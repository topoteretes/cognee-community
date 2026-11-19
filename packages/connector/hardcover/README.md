# Hardcover Books & Reading Journals Community Connector for Cognee

An enterprise GraphQL data-source connector integrating **Hardcover.app** with **Cognee**. Ingest personal libraries, reading statuses, book reviews, reading journals, and curated book lists into Cognee's cognitive memory graph.

## Features

- **Personal Books Library**: Ingests book titles, subtitles, page counts, author attributions, publication years, personal ratings, and detailed text reviews.
- **Reading Status Scoping**: Categorizes books across statuses: *Want to Read*, *Currently Reading*, *Read*, *Did Not Finish*, and *Owned*.
- **Reading Journals & Progress Tracking**: Ingests progress percentage, page milestones, and reading reflection notes.
- **Privacy Controls**: Strict opt-in toggle (`include_private=False` by default) ensures confidential notes and private journals remain shielded from memory graphs unless explicitly enabled.
- **Reading Lists**: Connects curated themed lists and reading sequences.
- **Hermetic Pytest Suite**: 100% offline mock test suite covering user identity scoping, GraphQL pagination, privacy filtering, and error handling.

## Installation

```bash
pip install cognee-community-connector-hardcover
```

## Quickstart

```python
import asyncio
from cognee_community_connector_hardcover import HardcoverConnector

async def main():
    # Hardcover Personal Access Token (hc_pat_...)
    token = "hc_pat_your_token_here"

    connector = HardcoverConnector(token=token, include_private=False)

    # Ingest library books, reading journals, and reading lists as Cognee documents
    documents = await connector.to_documents(
        include_books=True,
        include_journals=True,
        include_lists=True,
        limit=20
    )

    # Add directly to Cognee cognitive memory
    # await cognee.add(documents)
    # await cognee.cognify()

    await connector.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Running Tests

Execute the hermetic unit tests offline:

```bash
pytest packages/connector/hardcover/tests/test_hardcover.py -v
```
