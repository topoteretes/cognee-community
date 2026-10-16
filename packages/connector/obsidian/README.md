# cognee-community-connector-obsidian

An Obsidian vault data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your local markdown notes into memory — "ask my vault".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Notes
are rendered (frontmatter title/tags + body + a "Related notes" line built from
`[[wikilinks]]`) and ingested as **normal documents** (they flow through cognee's
cognify entity-extraction pipeline, not the deterministic dlt-row path), via cognee's
document-mode marker. No auth, no network — the vault is just files.

## Install

```bash
uv pip install cognee-community-connector-obsidian
# or, from this monorepo:
cd packages/connector/obsidian && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_obsidian import obsidian_source

await cognee.remember(
    obsidian_source("/path/to/vault"),
    dataset_name="obsidian",
)

answer = await cognee.search(
    query_text="What links to my project note?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["obsidian"],
)
```

Scope what you ingest with `include=[...]` globs (matched against the `/`-separated
path relative to the vault, e.g. `["projects/*.md"]`); omit it to sync every `.md`
file. `.obsidian/`, `.trash/`, and dot-directories are always skipped (override with
`exclude_dirs=[...]`). See `examples/example.py` for the full flow — it builds a tiny
demo vault, so it runs with no account and no existing notes.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging with
exactly the notes currently on disk on each run. A vault has no delete feed, so a
deleted (or renamed) note simply drops out of the snapshot and cognee's existing
`orphan_cleanup` removes it from the graph and vector stores. Unchanged notes keep a
stable content-hash `data_id` — touching a file without changing its text does not
re-ingest it — which is what makes re-syncs incremental in practice. A read error
aborts the run (leaving memory untouched) rather than letting a partial snapshot
forget live notes; malformed content (bad encoding, broken frontmatter) degrades
gracefully to plain text instead.

## Setup

None. Point `obsidian_source` at a vault directory and set your `LLM_API_KEY` like any
other cognee run.

## Testing

```bash
uv run pytest tests/
```

The tests build vaults in `tmp_path` (no real vault, no network) and cover frontmatter
parsing, title/tags fallbacks, wikilink extraction (aliases, sections, embeds,
attachment filtering), and full-snapshot forget-on-delete (edit / delete on re-sync).
