# cognee-community-connector-readwise

A Readwise data-source connector for [cognee](https://github.com/topoteretes/cognee).
It turns books and articles into normal cognee documents containing source context,
document notes, summaries, highlights, and highlight notes.

## Install

```bash
uv pip install cognee-community-connector-readwise
# or, from this monorepo:
cd packages/connector/readwise && uv sync
```

## Setup and usage

Create an access token at <https://readwise.io/access_token>, then set it alongside
the LLM credentials required by cognee:

```bash
export READWISE_ACCESS_TOKEN="..."
export LLM_API_KEY="..."
uv run python examples/example.py
```

Or construct the source directly:

```python
import cognee
from cognee_community_connector_readwise import readwise_source

await cognee.remember(
    readwise_source(),
    dataset_name="readwise",
    max_rows_per_table=0,
)
```

Pass `book_ids=[123, 456]` to restrict a dedicated dataset to specific Readwise
`user_book_id` values. Keep a dataset's scope stable between runs so records outside a
new, narrower scope do not remain in that dataset.

## Sync behavior

The first run exports the selected history. After complete pagination, the connector
stores the UTC sync-start time and passes it as `updatedAfter` on the next run. Capturing
the time before the request prevents updates made during pagination from falling between
sync windows. The cursor advances only after every page succeeds.

The connector always requests `includeDeleted=true`. Deleted books are emitted with
dlt's hard-delete marker; deleted highlights disappear from the rendered parent document.
Using merge preserves unchanged books during incremental runs, while cognee's orphan
cleanup removes hard-deleted documents from graph and vector stores.

Readwise documents use cognee's document ingestion route, so their prose is processed by
the normal cognify entity-extraction pipeline rather than the relational dlt-row path.

## Testing

```bash
uv run pytest tests/
```

The test suite is offline: it uses a fake HTTP session to cover pagination, filtering,
rendering, deletion rows, and incremental cursor safety.
