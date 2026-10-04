# cognee-community-connector-wordpress

WordPress data-source connector for [cognee](https://github.com/topoteretes/cognee): sync posts, pages, comments, and custom post types into memory — "ask my WordPress site".

It exposes a `dlt` resource that you hand directly to `cognee.remember(...)`. Posts, pages, and comments are converted into clean markdown/text and ingested as **normal documents** (flowing through cognee's cognify entity-extraction pipeline) via cognee's document-mode routing marker.

---

## Features

- **Authentication**: Connects securely via WordPress Application Passwords (HTTP Basic Auth).
- **Flexible Ingestion**: Ingests posts, pages, and comments by default, with full support for configurable custom post types (e.g. WooCommerce products, documentation).
- **Incremental Sync**: Uses WordPress REST API `modified_after` filter (and `after` for comments) to fetch only items modified since the previous sync run.
- **Forget-on-Delete**: Automatically identifies content deleted in WordPress via a lightweight ID sweep and emits hard-delete tombstones so cognee's `orphan_cleanup` purges them from the knowledge graph and vector store.
- **Self-Hosted & WordPress.com**: Works with self-hosted WordPress instances (`/wp-json/wp/v2`) and WordPress.com sites.

---

## Installation

```bash
pip install cognee-community-connector-wordpress
# or, from this monorepo:
cd packages/connector/wordpress && uv sync --all-extras
```

---

## Setup

1. **Generate an Application Password** in WordPress:
   - In WP Admin, go to **Users** → **Profile**.
   - Scroll down to the **Application Passwords** section.
   - Enter an application name (e.g., `cognee-connector`) and click **Add New Application Password**.
   - Copy the generated password.
2. Set your environment variables:
   ```bash
   export WORDPRESS_URL="https://your-wordpress-site.com"
   export WORDPRESS_USERNAME="your-username"
   export WORDPRESS_APP_PASSWORD="xxxx xxxx xxxx xxxx"
   export LLM_API_KEY="your-llm-api-key"
   ```

---

## Quickstart

```python
import asyncio
import cognee
from cognee_community_connector_wordpress import wordpress_source


async def main():
    # Ingest posts, pages, and comments
    await cognee.remember(
        wordpress_source(
            base_url="https://your-wordpress-site.com",
            username="your-username",
            app_password="xxxx xxxx xxxx xxxx",
            content_types=["posts", "pages", "comments"],
        ),
        dataset_name="wordpress_site",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )

    # Search cognee memory
    results = await cognee.search(
        query_text="What are the main topics discussed on my blog?",
        query_type=cognee.SearchType.GRAPH_COMPLETION,
        datasets=["wordpress_site"],
    )
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
```

See `examples/example.py` for a full runnable script.

---

## Custom Post Types

To ingest custom post types (e.g., custom plugins or custom taxonomies):

```python
wordpress_source(
    base_url="https://example.com",
    custom_post_types=["products", "portfolio"],
)
```

---

## How Sync and Forget-on-Delete Work

1. **Lightweight ID Sweep**: On each run, the connector queries item IDs (`_fields=id`) across the selected content types.
2. **Delta Detection**:
   - Items modified after the stored cursor (`modified_after`) are fetched and upserted.
   - Items present in `known_ids` on the previous run that are missing from the current sweep are emitted with `_deleted=True`.
3. **Graph Reconciliation**: `dlt` removes marked rows under `write_disposition="merge"`, and cognee's `orphan_cleanup` purges corresponding entities and embeddings from the graph and vector stores.

---

## Running Tests

Unit tests are fully mocked and do not require network calls or live credentials:

```bash
uv run pytest packages/connector/wordpress/tests/
```
