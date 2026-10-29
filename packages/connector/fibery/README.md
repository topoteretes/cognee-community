# cognee-community-connector-fibery

Sync selected Fibery databases into Cognee as markdown documents.

```bash
pip install cognee-community-connector-fibery
```

Create an API token in Fibery, then set `FIBERY_API_TOKEN` or pass `token=`.
The token is sent as `Authorization: Token <token>`.

```python
import cognee
from cognee_community_connector_fibery import fibery_source

await cognee.remember(
    fibery_source(account="your-account", databases=["Product/Feature"]),
    dataset_name="fibery",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```

Each selected database is scanned with keyset pagination. The source stores the
last modification date and complete entity id set per database, fetches changed
rich-text documents in batches, and emits hard-delete tombstones only after a
complete successful sweep. Entity relations and comments are rendered as
readable references/text. A failed or partial API read aborts the DLT run, so it
cannot erase previously synchronized entities.

## Example

Set `FIBERY_API_TOKEN` and `LLM_API_KEY`, then run `python examples/example.py`.
The example targets the `Product/Feature` database; change `FIBERY_DATABASES`
to a comma-separated list of database names in your workspace.
