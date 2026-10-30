# Crowdin Connector for Cognee

A production-grade [Cognee](https://github.com/topoteretes/cognee) community data-source connector for **[Crowdin](https://crowdin.com/)**, the enterprise localization and translation management platform used by GitHub, GitLab, Discord, and Microsoft open-source projects.

This connector ingests localization projects, approved terminology glossaries, source strings, and multilingual translations into structured Markdown documents optimized for Cognee's `cognify()` knowledge graph pipeline and semantic vector retrieval.

---

## Key Capabilities

- **Project Discovery**: Extracts project metadata, source languages, target locales, and web URLs.
- **Terminology Glossaries**: Ingests approved technical terms, prohibited synonyms, and concept definitions.
- **String & Translation Catalog**: Maps developer context descriptions, source keys, and target translations.
- **Document-Mode Routing**: Sets `DOCUMENT_SOURCE_ATTR = "crowdin"` ensuring documents route through Cognee's downstream cognitive extraction.
- **Orphan Cleanup (`write_disposition="replace"`)**: Automatically removes deleted strings or outdated glossary definitions from memory.
- **Deterministic Document IDs**: Uses stable `uuid5` hashing so subsequent syncs idempotently update existing memory nodes without duplicates.
- **Security & Token Masking**: Automatically redacts API tokens (`[REDACTED_CROWDIN_TOKEN]`) from logs, error messages, and exception traces.

---

## Installation

```bash
pip install cognee cognee-community-connector-crowdin
```

Or using `uv`:

```bash
uv add cognee cognee-community-connector-crowdin
```

---

## Configuration Reference

The connector is configured using `CrowdinConfig`:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `api_token` | `str` | *required* | Crowdin Personal Access Token (Bearer auth) |
| `project_ids` | `list[int] \| None` | `None` | Specific project IDs to ingest (or `None` for all accessible) |
| `organization_domain` | `str \| None` | `None` | Optional organization subdomain for Crowdin Enterprise |
| `include_glossaries` | `bool` | `True` | Whether to extract terminology glossaries and concept definitions |
| `include_strings` | `bool` | `True` | Whether to extract source strings, keys, and translations |
| `limit` | `int` | `50` | Pagination limit for string and term retrieval |
| `request_timeout` | `float` | `30.0` | HTTP request timeout in seconds |

---

## Quickstart

```python
import asyncio
import os
import cognee
from cognee_community_connector_crowdin import (
    CrowdinConfig,
    crowdin_connector,
)


async def main():
    config = CrowdinConfig(
        api_token=os.getenv("CROWDIN_PERSONAL_TOKEN", "crowdin_pat_example"),
        include_glossaries=True,
        include_strings=True,
    )

    # 1. Fetch localization telemetry
    documents = await crowdin_connector(config)
    print(f"Retrieved {len(documents)} localization documents.")

    # 2. Add to Cognee memory
    await cognee.add(documents, dataset_name="localization_knowledge")

    # 3. Cognify into knowledge graph
    await cognee.cognify(dataset_name="localization_knowledge")

    # 4. Query approved terminology
    query = "What is the approved German translation for Passkey?"
    results = await cognee.search(query, dataset_name="localization_knowledge")
    for r in results:
        print(f"Memory match: {r}")


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Running Offline Tests

The connector includes comprehensive offline unit tests that run with synthetic mocked HTTP transports (no live Crowdin account or network connection required):

```bash
uv run --project packages/connector/crowdin --with pytest --with pytest-asyncio pytest packages/connector/crowdin/tests -p no:typeguard -v
```

---

## License

Apache-2.0
