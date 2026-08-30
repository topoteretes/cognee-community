# cognee-community-connector-obsidian

An Obsidian vault data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your local vault into memory — "ask my vault".

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Notes are ingested as
**normal documents** (they flow through cognee's cognify entity-extraction pipeline, not
the deterministic dlt-row path) via cognee's document-mode marker. No auth — the vault is
a local directory and the connector only reads it.

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
    obsidian_source("~/Documents/MyVault"),  # or set OBSIDIAN_VAULT_PATH
    dataset_name="obsidian",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by note path
    max_rows_per_table=0,  # 0 = no row cap (vaults often exceed the default 50)
)

answer = await cognee.search(
    query_text="Summarize my project notes.",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["obsidian"],
)
```

See `examples/example.py` for the full flow.

## What gets ingested

- Every `*.md` file in the vault. **YAML frontmatter** becomes record metadata (a
  `metadata` JSON column, also folded into the note text as a `Properties` section so it
  survives into entity extraction); the body is the content.
- **Wikilinks become edges**, not discarded text — including the three forms plain
  markdown parsers miss:
  - `[[note]]` — plain link
  - `[[note|alias]]` — the edge target is `note`, not the alias
  - `[[note#heading]]` — the target is still `note`
  - `![[note]]` — an embed: an edge plus a transclusion flag

  Resolution is **name-based and vault-wide**, like Obsidian itself: `[[foo]]` resolves
  to a `foo.md` anywhere in the vault (case-insensitive; on ambiguity the shortest path
  wins, and `[[dir/foo]]` path links resolve by path). Resolved links are emitted as a
  structured `links` JSON column *and* rendered into the note text as explicit
  "Links to / Embeds" statements, which is what turns them into note→note edges in the
  extracted graph.
- **Excluded by default:** `.obsidian/`, `.trash/`, and `*.sync-conflict-*` files. The
  last one matters in practice: any vault synced with Syncthing/iCloud grows conflict
  copies that are near-duplicates of live notes, and ingesting them poisons dedup. Add
  `exclude=["drafts/*", ...]` for extra glob patterns.

## How incremental sync works

The cursor is a per-file **(mtime, size, sha256)** map kept in dlt's per-resource state.
`mtime` + `size` are the cheap fast path — an unchanged note is skipped without being
read. The hash is authoritative: sync tools (Syncthing, iCloud) rewrite mtimes and can
move them *backwards*, so any mtime change — forward or backward — falls through to the
content hash, and only a real change re-emits the note. A touch without an edit refreshes
the cursor silently.

The hash covers the *rendered* row rather than the raw bytes, so a note is also re-synced
when its link resolution changes — e.g. a previously unresolved `[[foo]]` starts
resolving because `foo.md` was just created. (When the vault's set of note names changes,
the fast path is disabled for that one run so re-resolution can happen; unchanged notes
are still not re-emitted.)

## How forget-on-delete works

The previous run's path snapshot lives in the same resource state. Paths absent from the
current walk are emitted as `_deleted` hard-delete tombstones; dlt removes those rows on
`merge` and cognee's existing `orphan_cleanup` purges them from the graph, vector, and
relational stores on the next sync.

**A rename is a delete + create.** Obsidian has no stable note id — the vault-relative
path *is* the identity — so renaming `A.md` to `B.md` forgets `A.md` and ingests `B.md`
fresh. This is by construction, not a bug.

As a safety valve, a walk that suddenly finds **zero** notes in a previously populated
vault (unmounted disk, permissions hiccup) skips deletion for that run instead of
tombstoning the whole dataset.

## Testing

```bash
uv run --with pytest pytest tests/
```

The tests need no live vault beyond `tmp_path` and cover the ingest path, all three
wikilink forms + embeds, name-based resolution (including ambiguity), the default
exclusions, the incremental cursor (including the mtime-moved-backwards regression),
deletion detection, and the dlt merge + hard-delete pipeline end to end.
