# cognee-community-connector-evernote

An Evernote data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Evernote notes into memory — "ask my Evernote".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`.
Evernote notes are flattened from ENML to text and ingested as **normal documents**
(they flow through cognee's cognify entity-extraction pipeline, not the deterministic
dlt-row path), via cognee's document-mode marker.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it. **This is not in cognee 1.3.0.** The `cognee==` pin in
> `pyproject.toml` is a placeholder; set it to the first release that includes
> document-mode before publishing.

> [!WARNING]
> **Evernote's Cloud API (EDAM) is officially deprecated.** Evernote's developer site
> marks the legacy API and its SDKs "no longer actively developed", developer tokens
> are issued "only for proven necessity", and new API keys are gated behind a manual,
> human-reviewed request (historically ~5 business days). This connector is written
> against that API because it is the only Evernote API that exposes note content —
> Evernote's newer MCP/REST surface is unannounced and unpublished. Expect friction
> obtaining credentials; the connector itself is fully exercised offline in `tests/`.

## Install

```bash
uv pip install cognee-community-connector-evernote
# or, from this monorepo:
cd packages/connector/evernote && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_evernote import evernote_source

await cognee.remember(
    evernote_source(),  # EVERNOTE_AUTH_TOKEN, or the token cached by examples/authorize.py
    dataset_name="evernote",
    primary_key="id",
    write_disposition="merge",  # REQUIRED — see below
    max_rows_per_table=0,  # 0 = no row cap
)

answer = await cognee.search(
    query_text="What did I decide about the migration?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["evernote"],
)
```

### `write_disposition="merge"` is mandatory

The add pipeline defaults to `"replace"` — drop the table and reload it on every run.
On the second, incremental sync that would wipe everything already ingested while the
cursor still points at a destination that no longer exists. Always pass `"merge"`.
`max_rows_per_table=0` avoids the default 50-row read cap, so orphan cleanup compares
against the whole ingested corpus rather than a truncated window.

### Selecting what to ingest

```python
evernote_source(
    notebook_guids=["a1b2..."],  # AND — only these notebooks
    tag_names=["research"],  # AND — only notes carrying every listed tag
)
```

`SyncChunkFilter` cannot filter by notebook or tag, so the selection is applied
client-side while scanning. Evernote's own search grammar (`query=`) is **not**
supported — it would require a second, separate scan engine.

The selection is hashed into the sync state. **Changing it resets the cursor** and
re-scans the account, so notes that dropped out of scope are forgotten rather than
lingering in memory.

## Setup

1. Request an Evernote API key (consumer key + secret) at
   <https://dev.evernote.com/portal/manage> — gated, manual review.
2. Run the OAuth 1.0a helper and paste back the verifier:

   ```bash
   uv run python examples/authorize.py
   ```

   It prints an authorization URL and caches the access token at
   `~/.cognee/evernote_token.json` (override with `EVERNOTE_TOKEN_PATH`).

   Skip OAuth entirely for your own account by setting `EVERNOTE_AUTH_TOKEN` to an
   Evernote **developer token** — EDAM takes both as the same opaque string.
3. Set your `LLM_API_KEY` like any other cognee run.

## How sync + forget-on-delete work

**Incremental.** The account's USN (update sequence number) is the cursor. Each run
captures the current USN as a target, then streams sync chunks until it reaches it.
The cursor, the ingested-id set and a hash of your selection live in dlt's
per-resource state, so re-running `remember` resumes where it left off and only
re-embeds the delta. Unchanged notes keep a stable content-hash `data_id`, so they
are not re-cognified.

**Forget-on-delete.** A deleted note simply stops appearing in later sync chunks, so
absence is not a signal. The connector uses Evernote's two explicit deletion feeds
instead:

| Upstream action | Feed | Effect |
|---|---|---|
| Delete (permanent) | `SyncChunk.expunedNotes` | note forgotten from the graph |
| Move to Trash | `Note.deleted` | note forgotten from the graph |

Evernote's Trash is a *real notebook*, so a naive full snapshot would happily
re-ingest trashed notes. Both cases emit a `_deleted` hard-delete marker; dlt drops
those rows on `merge` and cognee's `orphan_cleanup` purges them from the graph,
vector and relational stores. Restoring a note from Trash makes it live again on a
later chunk, which re-ingests it.

Deletion is also guarded: if a full re-scan returns zero notes while notes were
previously known, the run skips deletion rather than purging everything on what is
almost always a transient failure.

## Testing

```bash
uv sync
uv run pytest tests/ -q
```

The suite runs entirely offline against a fake Thrift client — no Evernote
credentials, no network, no LLM:

| File | Proves |
|---|---|
| `tests/test_evernote.py` | ENML rendering, the sync state machine, the cursor, both deletion feeds, scope reconciliation, the empty-scan guard, retry/error classification, and the dlt resource wiring |
| `tests/test_evernote_dlt.py` | a real `dlt` pipeline over two runs: the tombstone row is physically removed from the destination |
| `tests/test_evernote_ingestion.py` | the real `cognee.add()` path: one Data record per note, `source="evernote"`, stable `data_id` for unchanged notes, forgotten deleted notes |
| `tests/test_evernote_forget.py` | `add()` + `cognify()` with LLM/embeddings mocked: a deleted note's extracted entity is **gone from the graph** while the surviving note's remains |
| `tests/test_evernote_live.py` | opt-in live smoke test, skipped unless `EVERNOTE_LIVE=1` |

### Does the suite actually validate?

A green suite proves nothing if it cannot fail. `mutation_check.py` injects each
deliberate defect below into the connector and requires the suite to catch it:

```bash
uv run python mutation_check.py
```

| Injected defect | Caught by |
|---|---|
| drop expunged (permanently deleted) notes | `test_expunged_note_is_tombstoned_incrementally` |
| ignore the Trash flag | `test_trashing_a_note_tombstones_it_rather_than_reingesting_it` |
| always full-scan (never resume from the cursor) | `test_second_sync_with_no_changes_yields_nothing` |
| never treat the first run as a full scan | `test_narrowing_the_notebook_scope_tombstones_dropped_notes` |
| delete the empty-scan guard | `test_empty_full_rescan_does_not_mass_delete` |
| ignore `notebook_guids` | `test_in_scope_filters_by_notebook` |
| `write_disposition="replace"` | `test_source_resource_is_configured_for_merge_and_hard_delete` |
| drop the `_deleted` `hard_delete` marker | `test_source_resource_is_configured_for_merge_and_hard_delete` |
| stop declaring document mode | `test_source_declares_document_mode` |
| stop flattening ENML | `test_render_enml_strips_tags_and_unescapes` |

The script restores the original file and re-runs the suite, so it is safe to run
against a working tree.

## Live smoke test

With real credentials configured:

```bash
EVERNOTE_LIVE=1 uv run pytest tests/test_evernote_live.py -q -s
```

## Out of scope

- **Attachments / resources.** Sync chunks carry resource *metadata*; fetching
  binaries, ENEX bundles or OCR data is not implemented. An `[attachment: ...]`
  marker is rendered inline where the resource sat.
- **Evernote search-grammar scoping** (`query=`). See above.
- **Note content updates in place** — an edited note gets a new content hash and is
  re-ingested, which is correct, but it does not reuse the previous graph nodes.

## License

BSD-3-Clause, matching the Evernote EDAM API it targets.
