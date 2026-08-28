# cognee-community-connector-notion

A Notion data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Notion workspace into memory.

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Notion
pages are rendered to markdown and ingested as **normal documents** (they flow through
cognee's cognify entity-extraction pipeline, not the deterministic dlt-row path), via
cognee's document-mode marker.

## Requirements

This connector requires a cognee release that ships "document-mode" i.e.
`cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
routing that reads it. The `cognee==` pin in `pyproject.toml` is a placeholder; set it
to the first release that includes document-mode before publishing.

## Install

```bash
uv pip install cognee-community-connector-notion
# or, from this monorepo:
cd packages/connector/notion && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_notion import notion_source

await cognee.remember(
    notion_source(),  # NOTION_API_KEY from env, or pass token=...
    dataset_name="notion",
)

answer = await cognee.search(
    query_text="Summarize my project notes.",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["notion"],
)
```

Scope what you ingest with `page_ids=` or `database_ids=`:

```python
notion_source(page_ids=["abc123", "def456"])
notion_source(database_ids=["my-db-id"])
```

## Incremental Sync (Watermark)

### How it works

Every sync performs a **full enumeration** of all pages visible to the integration
(cheap paginated `search` / `databases.query` calls). This enumeration is not
skipped because it is **load-bearing**: the full-snapshot `write_disposition="replace"`
model means that any page missing from a run would be treated as deleted and
forgotten from the graph. The listing stays complete so `orphan_cleanup` can
detect deletions correctly.

What *is* skipped on unchanged pages is the expensive part: the recursive
`blocks.children.list` API calls inside `_render_blocks`. After a successful
run the connector saves a **watermark store** (a JSON file keyed by page id)
that records each page last_edited_time and its rendered markdown content.

On the next run, for each page:

- **If last_edited_time matches the watermark**: the cached markdown is reused.
  Zero `blocks.children` API calls are made for that page.
- **If last_edited_time has advanced** (or the page is new): `_render_blocks` is
  called as normal and the watermark is updated.

The watermark is written **atomically** (write to a temp file, then `os.replace`)
only after the full snapshot completes without error. A render failure propagates
the exception, leaves staging untouched, and leaves the watermark from the
previous good run in place so the next attempt starts from a clean state.

### Watermark file location

Default path: `~/.cognee/notion_watermark.json`

If the `COGNEE_HOME` environment variable is set, the path becomes `$COGNEE_HOME/notion_watermark.json`.

Override by passing `watermark_path=` to `notion_source()`:

```python
# Use a custom path
notion_source(watermark_path="/data/my_notion_wm.json")

# Disable watermarking entirely (always do a full block-fetch)
notion_source(watermark_path=False)
```

### Cold start

When the watermark file does not exist (first run, or after deleting it), the
connector behaves identically to the original version: all pages are enumerated
and all blocks are fetched. The watermark is then created for subsequent runs.

### Deletion still works

Deleting, trashing, or unsharing a page causes Notion to omit it from
`search` / `databases.query` results. Since the connector still enumerates
every visible page on every run, the deleted page row is simply absent from
the new snapshot. With `write_disposition="replace"`, `orphan_cleanup` then
removes it from the graph and vector stores - regardless of whether it had a
cached watermark entry.

### The content-hash data_id is unchanged

`last_edited_time` is used only as a **block-fetch filter**, not as `data_id`.
The `data_id` is still a content hash (owned by cognee ingestion layer), so:

- A metadata-only edit that bumps `last_edited_time` without changing text
  causes a re-render but the content hash remains stable - no re-cognification.
- Only genuine text changes produce a new `data_id` and trigger re-cognification.

### Example log output

```
Notion: synced 142 page(s): 3 rendered, 139 skipped (watermark hit).
Notion: watermark saved to /home/user/.cognee/notion_watermark.json.
```

## Auth

The connector supports Notion internal integration token (the default for private/self-hosted use):

```bash
export NOTION_API_KEY=secret_...
```

Or pass it directly:

```python
notion_source(token="secret_...")
```

OAuth (public integrations): Notion public OAuth for a hosted "Connect your Notion"
experience is not yet implemented in this connector. See issue 4541 for tracking.
Contributions welcome as a follow-up PR.

## How deletion works

Notion has no delete feed. Archived, trashed, and unshared pages are simply
omitted from `search` / `databases.query` results rather than returned
with a deletion flag. This connector uses a full-snapshot model:

1. Each sync replaces staging with exactly the pages currently visible.
2. A page that disappears falls out of the snapshot.
3. cognee orphan_cleanup removes it from the graph and vector stores.

This is why `write_disposition="replace"` is used (not `merge`) and why the
connector does not switch to `hard_delete` - Notion cannot report deletions
in a way that makes `hard_delete` reliable.

## Running the tests

```bash
cd packages/connector/notion
uv sync --all-extras
pytest tests/ -v
```
