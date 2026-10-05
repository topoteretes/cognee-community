# cognee-community-connector-discourse

Discourse data-source connector for cognee: syncs forum topics into memory as documents,
with full-snapshot sync and forget-on-delete.

```python
from cognee_community_connector_discourse import discourse_source

source = discourse_source(base_url="https://meta.discourse.org", category_ids=[67])
await cognee.remember(source, dataset_name="discourse")
```
