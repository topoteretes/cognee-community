# cognee-community-connector-chatgpt

A ChatGPT **conversation-export** data-source connector for [cognee](https://github.com/topoteretes/cognee):
turn your ChatGPT history into memory — "ask my ChatGPT".

It reads the official ChatGPT data export (the `.zip`, or the extracted folder) into one
document per conversation and exposes a `dlt` source you hand to `cognee.remember(...)`.
Conversations are ingested as **normal documents** via cognee's document-mode marker (they
flow through cognify entity extraction), and the source reuses cognee's existing DLT
ingestion path (`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`), so you get
**snapshot sync** and **forget-on-delete** with no core changes.

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
    chatgpt_source("/path/to/export.zip"),  # or the extracted export folder
    dataset_name="my-chatgpt-history",
)

answer = await cognee.search(
    query_text="What did I decide about the database migration?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["my-chatgpt-history"],
    session_id="chatgpt-qa",  # see note below
)
```

| Option | Default | Effect |
|---|---|---|
| `include_do_not_remember` | `False` | Chats you excluded from ChatGPT's own memory (`is_do_not_remember`) are skipped unless set. |
| `include_archived` | `True` | Archiving only hides a chat from the sidebar; set `False` to leave archived chats out. |
| `allow_deletes` | `False` | Skip the stale-export guard (below) and trust this export as authoritative. |

> **Pass a `session_id` when asking questions.** Without one, every `search`/`recall` call
> shares cognee's `"default_session"`, whose last Q&A turns are added to the prompt — answers
> from unrelated earlier questions can leak in. This is cognee behaviour, not specific to
> this connector.

See `examples/example.py` for a runnable two-snapshot demo (synthetic export, no real data
needed), `examples/verify_forget.py` for proof that a deleted conversation leaves the
retrieval context, and `examples/verify_stale_guard.py` for the stale-export guard.

## What gets stored

One document per conversation:

- the title, plus `Project: g-p-…` / `Custom GPT: g-…` header lines (from
  `conversation_template_id` / `gizmo_id`) so chats in the same project share an identifier
- your custom instructions, if present
- every user and assistant message on the branch you kept (walking `current_node` → root),
  so abandoned edit/regenerate branches are never included
- images as `[image: <DALL·E prompt, if any>]`, voice-mode turns as their transcription

**Not stored, by design:** tool plumbing (code interpreter, browsing, canvas/DALL·E tool
calls), scraped web pages (`sonic_webpage`), model reasoning (`thoughts` /
`reasoning_recap`), and raw image/audio bytes.

## How sync + forget-on-delete work

An export is a **full snapshot**, so the source uses `write_disposition="replace"` (cognee's
document-mode default): every sync yields every conversation, and a conversation deleted in
ChatGPT drops out of the snapshot and is removed by cognee's `orphan_cleanup` from the graph,
vector and relational stores. Rendering is a pure function of the data, so an unchanged
conversation keeps its content-hash `data_id` and is **not** re-cognified. Leave
`write_disposition` at the default — `merge` breaks forget-on-delete for a snapshot source.
Run `remember()` in the foreground: cognee skips `orphan_cleanup` on background runs.

**Stale-export guard.** Pointing the connector at an old or truncated export would otherwise
look like "everything newer was deleted". The connector keeps `{conversation_id:
update_time}` in dlt source state, keyed by cognee dataset and the account id from
`user.json`. If a previously synced conversation is missing **and** was updated more recently
than anything in the new export, the sync raises `StaleExportError` before writing anything,
leaving memory untouched. A wrong guess costs a skipped sync, never lost memory; pass
`allow_deletes=True` to override.

## Setup

1. In ChatGPT: **Settings → Data controls → Export data → Export**.
2. Download the `.zip` from the email (the link expires after 24 hours) and point
   `chatgpt_source()` at it — no need to unzip.
3. Set your `LLM_API_KEY` like any other cognee run.

([OpenAI's export guide](https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data))

## Testing

```bash
uv run pytest tests/
```

Tests build synthetic exports in `tmp_path` (no real export or API key needed) and cover tree
reconstruction (edit branches), filtering, rendering, zip/folder/split-file discovery, and
`dlt` replace runs proving edits are re-synced, deleted conversations are physically removed,
and the stale-export guard refuses old exports without touching existing data.

To also verify every behaviour against **your own export** (opt-in; nothing personal is
committed and only structure is asserted):

```bash
CHATGPT_REAL_EXPORT_PATH=/path/to/export.zip uv run pytest tests/ -v
```
