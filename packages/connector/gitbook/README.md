# GitBook Connector

A Cognee connector for ingesting pages and content from a GitBook space.

## Features

- Authenticate with a GitBook API token.
- Ingest pages and their document content.
- Flatten GitBook's structured document tree into text/Markdown.
- Detect changes using the GitBook space revision ID.
- Skip ingestion when the revision has not changed.
- Propagate deleted pages during synchronization.

## Installation

From this directory:

```bash
uv sync