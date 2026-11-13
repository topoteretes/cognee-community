# cognee-community-connector-fibery

Sync selected Fibery databases into Cognee as markdown documents.

```bash
pip install cognee-community-connector-fibery
```

Create an API token in Fibery and set `FIBERY_API_TOKEN`, or pass `token=`.
The token is sent as `Authorization: Token <token>`.

```python
import cognee
from cognee_community_connector_fibery import fibery_source

await cognee.remember(
    fibery_source(account="your-account", databases=["Product/Feature", "CRM/Customer"]),
    dataset_name="fibery",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```

Each database is read with keyset pagination on `fibery/id`. A later run fetches
rich text only for entities whose `fibery/modification-date` moved, and keeps
the id set of each database. After every selected database has been read,
missing ids are emitted as `_deleted` tombstones. A failed page or document
read raises and emits none.

`account` is the workspace subdomain in `https://<account>.fibery.io`.
Database names are `Space/Database`.

## Example

Set `FIBERY_API_TOKEN`, `FIBERY_ACCOUNT`, `FIBERY_DATABASES` (comma-separated),
and `LLM_API_KEY`, then run `python examples/example.py`.

## Tests

```bash
pytest tests
```

The tests use an in-memory Fibery client. They cover paging, markdown, an
entity with no rich-text field, a short 429 retry, and tombstones only after
a complete read.
