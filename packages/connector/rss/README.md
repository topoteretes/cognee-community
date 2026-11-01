# cognee-community-connector-rss

RSS, Atom, and JSON Feed data-source connector for [cognee](https://github.com/topoteretes/cognee).

Ingests public blog posts, changelogs, newsletters, and syndication feeds into cognee memory as connected documents.

## Usage

```python
import cognee
from cognee_community_connector_rss import rss_source

await cognee.remember(
    rss_source([
        "https://openai.com/news/rss.xml",
        "https://github.blog/feed/",
    ]),
    dataset_name="tech-news",
)
```

## Features

- **Universal format support**: Parses RSS 2.0, RSS 1.0 (RDF), Atom feeds, and JSON Feed specs.
- **HTML sanitization**: Converts CDATA and raw HTML tags into clean, LLM-ready prose text.
- **Deduplication**: Deterministic GUID/URL hashing ensures repeat ingestion does not duplicate graph entities.
- **Zero heavy dependencies**: Implemented using standard Python XML parsing with `requests`.
