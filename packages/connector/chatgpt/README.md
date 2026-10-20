# cognee-community-connector-chatgpt

A ChatGPT **conversation-export** data-source connector for
[cognee](https://github.com/topoteretes/cognee): turn your ChatGPT history into
memory - "ask my past conversations".

It reads the official ChatGPT data export (Settings -> Data controls -> Export
data), reconstructs each conversation along the branch you actually kept (edits
and regenerated answers create branches cognee should never see), and exposes a
`dlt` source you hand to `cognee.remember(...)`. It reuses cognee's existing DLT
ingestion path (`resolve_dlt_sources` -> `ingest_dlt_source` -> `orphan_cleanup`),
so you get **snapshot sync**, **forget-on-delete**, and a **stale-export guard**
with no core changes.

## Install

```bash
uv pip install cognee-community-connector-chatgpt
# or, from this monorepo:
cd packages/connector/chatgpt && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_chatgpt import chatgpt_source

await cognee.remember(
    chatgpt_source("/path/to/export.zip"),  # or an already-extracted export folder
    dataset_name="my-chatgpt-history",
)

answer = await cognee.search(
    query_text="What did I decide about the database migration?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["my-chatgpt-history"],
)
```

See `examples/example.py` for a runnable two-snapshot demo (synthetic fixtures,
no real export needed), `examples/verify_forget.py` for a definitive proof
that a deleted conversation actually leaves retrieval context (not just a
database table), and `examples/verify_stale_guard.py` for the same against
the stale-export guard below.

## Getting your export

1. In ChatGPT: Settings -> Data controls -> Export data -> Export.
2. ChatGPT emails a download link within a few minutes (can take up to 7 days
   for very large accounts). The link expires 24 hours after it arrives.
3. Download the `.zip` while signed in to the same account, and point
   `chatgpt_source()` at it directly - no need to unzip it yourself.

(Official steps: <https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data>)

## What gets stored

For every conversation that is **not** excluded (see below), one document is
created with:

- the conversation title
- every **user** and **assistant** message on the branch you ended up with,
  in order
- the custom-GPT / project a conversation belongs to, if any (`gizmo_id` /
  `conversation_template_id`), as a one-line header

**Left out, always:** system/tool plumbing, the model's internal "thoughts" /
reasoning summaries, browsing-status noise, and raw image/file bytes (an image
is stored as `[image: <the DALL·E prompt, if any>]` - the prompt is text you
wrote, the pixels are not).

**Excluded by default, configurable:**

| Option | Default | Why |
|---|---|---|
| `include_do_not_remember=False` | excluded | ChatGPT's own "exclude from memory" flag on a conversation. A *different* memory system respecting that flag by default seemed like the safer default; pass `True` to include them anyway. |
| `include_archived=True` | included | Archiving only hides a conversation from ChatGPT's sidebar - it isn't a delete. Pass `False` if you'd rather leave archived chats out. |
| `allow_deletes=False` | guard on | See [Stale-export guard](#stale-export-guard) below. Pass `True` to skip it and trust this export as authoritative no matter what. |

## How sync + forget-on-delete work

Each export is a **full snapshot** of your account's conversations at the
moment you requested it - not a live feed - so the source uses
`write_disposition="replace"` (cognee's document-mode default too): every
export re-yields every conversation it contains, and cognee's `orphan_cleanup`
removes anything that dropped out since the last sync (deleted in ChatGPT, or
newly excluded by the filters above) from the graph + vector + relational
stores. Leave `write_disposition` at the default - passing `merge` would break
forget-on-delete for a snapshot source.

A conversation's rendered text is a pure function of its data, so an
**unchanged** conversation renders byte-identical text on every sync, keeps the
same content-hash `data_id`, and is **not** re-cognified - only new or edited
conversations cost an LLM call.

### Stale-export guard

Because a `replace` snapshot deletes anything it doesn't re-yield, pointing
this connector at an **old or truncated** export by mistake would otherwise
look identical to the user having deleted everything missing from it. To
guard against that: every conversation id this connector has ever seen is
remembered, per ChatGPT account (keyed by `user.json`'s own account id), along
with its `update_time`. Before a sync is allowed to commit, if any previously
seen conversation is **missing from the new export** *and* its recorded
`update_time` is **newer than everything currently in the export**, the sync
is refused with `StaleExportError` before anything is written — existing
memory is left exactly as it was:

```python
from cognee_community_connector_chatgpt import StaleExportError, chatgpt_source

try:
    await cognee.remember(chatgpt_source("/path/to/export.zip"), dataset_name="my-chatgpt-history")
except StaleExportError as exc:
    print(f"Refused: {exc}")  # re-export a current copy, or pass allow_deletes=True
```

A wrong guess here costs a skipped sync, never lost memory — if you're certain
the export is current (or deliberately rolling back to an older one), pass
`allow_deletes=True` to skip the check entirely. Conversations with no
`update_time` at all can't be checked and are treated as never-stale, so an
ordinary deletion is never blocked by this guard; see
`examples/verify_stale_guard.py` for a real end-to-end run proving the block
and the "left untouched" guarantee against a live `cognee.remember()` call,
not just the underlying `dlt` table.

### Known limitations (v1)

- Tool/code-interpreter output and web-search results are not kept - only
  conversational text.
- Canvas documents (`textdocs/` in the export) are not read.
- Orphan cleanup only runs on a **foreground** `cognee.remember(...)` /
  `cognee.add(...)` call. `run_in_background=True` skips it (cognee logs a
  warning) - deletions from your export will not propagate to memory on a
  background run. Run synchronously if you need deletions to take effect.
- The stale-export guard's account scoping needs `user.json` in the export
  (present in every official export observed so far). If it's absent, the
  guard still runs, just without per-account separation (logged once).

## Testing

```bash
uv run pytest tests/
```

> **Windows + a managed/corporate device:** if `uv run` or `uv venv` fails with
> `An Application Control policy has blocked this file. (os error 4551)`, your
> device's Windows Defender Application Control / AppLocker policy is blocking
> `uv`'s self-downloaded Python build specifically (not this connector, not your
> shell, not conda). The fix is to build the venv on an already-trusted
> interpreter instead of letting `uv` download its own:
> `uv venv .venv --python "C:\path\to\trusted\python.exe"` (e.g. your conda
> installation's `python.exe`), then `uv pip install -e . pytest`.
>
> **If you then hit `Could not find lbug C API shared library`** (only when
> actually running `cognee.remember()`/`cognee.cognify()`, not during
> `pytest`): the `ladybug` graph-database package's native shared library
> isn't bundled for every Python/platform wheel combination. Download the
> matching release directly from
> [LadybugDB/ladybug releases](https://github.com/LadybugDB/ladybug/releases)
> — match the exact version installed (`pip show ladybug`), grab
> `liblbug-windows-x86_64.zip` (or your OS's equivalent), and extract
> `lbug_shared.dll` into a `lib/` folder next to where you run Python from
> (or set `LBUG_C_API_LIB_PATH` to point at it directly). This is a `ladybug`
> packaging gap, unrelated to this connector.

Tests build synthetic conversation exports in `tmp_path` (no real export or
API access needed) and include an offline end-to-end run driving the source
through a real `dlt` replace to prove a conversation dropped between snapshots
is physically removed - exactly what cognee's `orphan_cleanup` reconciles
against.
