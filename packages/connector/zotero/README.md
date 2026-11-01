# cognee-community-connector-zotero

Sync Zotero bibliographic records, notes, and attachment metadata/indexed text
into cognee as normal documents through its document-mode ingestion path.

## Install

Requires Python 3.11–3.13 and cognee 1.4.0.

```bash
cd packages/connector/zotero
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e . pytest ruff httpx
```

## Setup

Get your numeric userID and a read-enabled API key from
[Zotero API settings](https://www.zotero.org/settings/keys). For a personal library,
pass `library_type="user"`, `library_id="YOUR_USER_ID"`, and set `ZOTERO_API_KEY`
or pass `zotero_api_key=`. Private groups require a key with group read access.

Public groups need neither a key nor a userID: use the numeric group ID from the
group URL. The example defaults to the long-established
[VSG public library](https://www.zotero.org/groups/479046/vsg_public/items/),
`479046`; override with `ZOTERO_GROUP_ID`. Public-library availability and size
can change. Set your usual cognee `LLM_API_KEY` for graph ingestion/search.

## Usage

```python
import cognee
from cognee_community_connector_zotero import zotero_source, ZoteroLibraryUnchanged

try:
    await cognee.remember(zotero_source(library_id="479046"), dataset_name="zotero")
except ZoteroLibraryUnchanged as exc:
    print(f"Library unchanged since version {exc.version}; nothing to ingest.")

answer = await cognee.search(
    query_text="Summarize the research in this library.",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["zotero"],
)
```

Run `python examples/example.py` for the complete public-group example.

The single `documents` resource uses `primary_key="id"` and
`write_disposition="replace"`. Stable IDs are
`zotero://<library_type>/<library_id>/items/<key>`. Rows carry title/content plus
flat item_key, item_type, version, doi, url, pub_date, journal, creators (JSON
string), tags and collections (comma-joined). Notes use their own key, stripped
HTML, and parent title. Attachments include filename, content type, link mode,
parent title, and any indexed fulltext.

Options: required `library_id`; `library_type="group"`; `zotero_api_key=None`
(falls back to `ZOTERO_API_KEY`); `include_notes=True`;
`include_attachment_text=True`; `page_size=100`; `pacing_seconds=0.3`.
An optional injected `httpx.Client` remains caller-owned. An injected `clock`
provides `monotonic()` and `sleep(seconds)` for testing.

## Watermark & incremental sync

This is a full snapshot with a version-watermark skip. Watermarks persist in dlt
SOURCE state, scoped to the latest library and rendering options. Changing
rendering options forces a fresh snapshot. Reuse the same persistent
pipeline/state and dataset. If the pipeline is not active at source construction,
the probe is deferred until extraction can read its persisted state.
The first round skips the probe. Subsequent rounds probe
`/items?limit=1&format=json` with `If-Modified-Since-Version: <watermark>`.

A 304 raises `ZoteroLibraryUnchanged` during source construction when state is
available, otherwise during extraction before any rows are yielded or loaded. Catch this as the no-op signal: staging and the cognee graph
remain untouched. An empty yield would truncate a replace table and is never
used for an unchanged library. A genuinely empty library replaces staging with
zero rows.

Changed rounds buffer every item page, fetch collections once, and render all
rows before yielding. Incomplete counts, duplicate items, changing library
versions, incomplete collections, or failed requests raise `ZoteroSyncError`.
The watermark advances only after successful extraction; dlt rolls back state
on failed runs. Extraction errors may be wrapped in a dlt pipeline exception,
with `ZoteroSyncError` or `ZoteroLibraryUnchanged` in the cause chain. The runnable
example unwraps the latter before catching it as the no-op signal.

True incremental sync is an upgrade path: combine `since=<version>` with
`GET /users/<id>/deleted?since=<version>` (or `/groups/<id>/deleted`). The deletion
log contains collections, searches, items, tags, and settings. v1 does not use it.

## Deletions

A successful snapshot replaces staging. The snapshot diff identifies absent
IDs, and cognee's existing `orphan_cleanup` removes their graph/vector data.
Deleted, trashed, or no-longer-visible items disappear from the listing. Trash
is excluded by the API; no `include_trashed` option is offered. Unchanged rows
retain stable content hashes, avoiding unnecessary re-ingestion. Failed or 304
rounds cannot trigger cleanup. Keep separate libraries in separate pipelines and
datasets because replace is authoritative for the chosen library.

## Limits

- `page_size` must be an integer from 1 through 100; larger values are rejected.
- Full snapshots are buffered in memory. Collections are fetched once; an
  incomplete/paginated collection response aborts rather than losing names.
- Fulltext is best-effort for stored attachments (`imported_file`/`imported_url`).
  A 404 or absent content produces a logged metadata-only document. Other
  permanent errors or exhausted retries abort the snapshot.
- No binary downloads or `/file` calls. Linked files/URLs remain metadata-only.
- Sequential requests default to 0.3-second pacing. `Backoff` delays the next
  request; 429 honors `Retry-After`, otherwise exponential backoff is used.
  Transient HTTP/transport failures get at most five attempts. Library 403/404
  responses fail immediately with a clear error.

## Testing

```bash
source .venv/bin/activate
ruff check .
pytest
```

Tests use httpx MockTransport, an injectable clock, blocked sockets (including
import-time networking), and real temporary SQLite dlt staging. They cover
rendering, headers, retries, pagination, document routing, deletions, staging
preservation on 304/failure, and watermark rollback without external services.
