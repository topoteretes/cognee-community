# cognee-community-connector-salesloft

A Salesloft data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your sales engagement data into memory — "ask my sales pipeline".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Salesloft
notes, calls, and cadences are rendered to markdown and ingested as **normal documents**
(they flow through cognee's cognify entity-extraction pipeline, not the deterministic
dlt-row path), via cognee's document-mode marker.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it. The `cognee==` pin in `pyproject.toml` targets cognee 1.4.0.

## Install

```bash
uv pip install cognee-community-connector-salesloft
# or, from this monorepo:
cd packages/connector/salesloft && uv sync --all-extras