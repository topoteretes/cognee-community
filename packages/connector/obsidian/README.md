# cognee-community-connector-obsidian

An Obsidian vault data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your local vault into memory — "ask my vault".

It exposes a `dlt` resource you hand to `cognee.remember(...)`. Notes are ingested as
**normal documents** (they flow through cognee's cognify entity-extraction pipeline, not
the deterministic dlt-row path) via cognee's document-mode marker. No auth — the vault is
a local directory and the connector only reads it.

Requires cognee 1.6.3 or newer.

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

### Options

| Argument | Default | What it does |
|---|---|---|
| `vault_path` | `OBSIDIAN_VAULT_PATH` | The vault directory |
| `exclude` | `None` | Extra glob patterns to skip, matched against vault-relative paths |
| `resource_name` | `"obsidian_notes"` | Table name and the scope of the incremental state |
| `check_active` | `None` | Host callback run around every extraction step; raise in it to stop the sync |
| `allow_empty` | `False` | Confirm that a vault with zero notes really is empty, so its notes are forgotten |

**Several vaults in one dataset need one `resource_name` each**, for example
`obsidian_source(work, resource_name="work_vault")` and
`obsidian_source(home, resource_name="home_vault")`. Two vaults under one name would
share a cursor and tombstone each other's notes.

After a run, `source.cognee_sync_stats` holds counts only: `seen`, `changed`, `deleted`,
`skipped`, `unreadable`.

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

A note modified within two seconds of the previous sync is always hashed, never trusted
to the fast path. On filesystems with coarse timestamps (FAT/exFAT, some network and
virtualized mounts) a same-size edit made right after a sync leaves `mtime` and `size`
identical and would otherwise be missed. Git guards its index the same way.

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

**Nothing is forgotten on an incomplete scan.** If a directory cannot be listed
(permissions, a cloud folder that is offline), the whole run is skipped and the state is
left as it was. If a single note cannot be read, or vanishes for an instant while an
editor or sync tool saves it, that note keeps its place and deletion is skipped for that
run. The same goes for a walk that suddenly finds **zero** notes in a previously
populated vault, which is what an unmounted disk looks like. If you really emptied the
vault, pass `allow_empty=True` for that one run rather than leaving it on.

## Testing

```bash
uv sync --group dev
uv run pytest tests/
```

The tests need no live vault beyond `tmp_path` and cover the ingest path, all three
wikilink forms + embeds, name-based resolution (including ambiguity), the default
exclusions, the incremental cursor (including the mtime-moved-backwards and
same-size-edit regressions), deletion detection, per-vault resource scoping, the
`check_active` stop, and the dlt merge + hard-delete pipeline end to end.
