# Zotero connector for cognee

Sync your Zotero library — references, notes, and attachment text —
into cognee with **version-based incremental sync** and **forget-on-delete**.

Auth is a personal API key (no OAuth dance).

## One-time setup

1. Install the extra:

       pip install "cognee[zotero]"

2. Create a personal API key at https://www.zotero.org/settings/keys
   and share your library with it (or use a group API key).
3. Export the key and your LLM key:

       export ZOTERO_API_KEY="..."
       export LLM_API_KEY="sk-..."
4. Run the example:

       uv run python examples/example.py

Re-run after adding or deleting items to see the incremental sync and
forget-on-delete in action.

## Usage

```python
import cognee
from cognee_community_connector_zotero import zotero_source

await cognee.remember(
    zotero_source(),
    dataset_name="my_zotero",
    primary_key="id",
    write_disposition="merge",
)
```

Pass `api_key="..."` explicitly, or leave it blank to read ``ZOTERO_API_KEY``.
Pass ``user_id=`` to skip the automatic ``/keys/current`` lookup.

## How it works

* **Incremental sync** — each run records the library's ``Last-Modified-Version``
  and only fetches items changed since then (cheap on large libraries).
* **Forget-on-delete** — deleted or trashed items are emitted as ``_deleted``
  tombstones; dlt's ``merge`` + ``hard_delete`` removes them from staging and
  cognee's ``orphan_cleanup`` forgets them from the graph and vector stores.
* **Document mode** — items flow through cognee's normal cognify pipeline
  (LLM entity extraction), not the relational dlt path.
