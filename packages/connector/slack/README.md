# cognee-community-connector-slack

A Slack **workspace-export** data-source connector for [cognee](https://github.com/topoteretes/cognee):
turn a Slack export archive into memory — "ask our Slack".

It parses the standard Slack export layout (`channels.json`, `users.json`, and per-channel daily
JSON message files) into flat message rows and exposes a `dlt` source you hand to
`cognee.remember(...)`. It reuses cognee's existing DLT ingestion path
(`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`), so you get **snapshot sync**
and **forget-on-delete** with no core changes.

## Install

```bash
uv pip install cognee-community-connector-slack
# or, from this monorepo:
cd packages/connector/slack && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_slack import slack_export_source

await cognee.remember(
    slack_export_source("/path/to/slack-export"),
    dataset_name="team-slack-export",   # use a DEDICATED dataset (see below)
    max_rows_per_table=0,               # ingest the whole export
)

answer = await cognee.search(
    query_text="What did we decide about the launch date?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["team-slack-export"],
)
```

See `examples/example.py` for a runnable two-snapshot demo (fixtures in `examples/test_data/`).

## How sync + forget-on-delete work

A Slack export is a **full snapshot**, so the source uses `write_disposition="replace"` (cognee's
default): each sync drops + reloads the table, a message removed upstream falls out of the current
set, and cognee's `orphan_cleanup` purges it from the graph + vector + relational stores. Message
ids (`{channel_id}:{ts}`) are stable, so re-ingesting a later export only re-cognifies
new/changed messages. Leave `write_disposition` at the default — passing `merge` breaks
forget-on-delete for a snapshot source.

> **Use a dedicated `dataset_name` per workspace.** On released cognee, `orphan_cleanup` scopes
> deletions per dlt source within a dataset; a dedicated dataset guarantees cleanup only ever
> touches this export. (A core enhancement adds table-scoped orphan cleanup so multiple dlt
> sources can safely share one dataset — see the note in the PR.)

Only public channels in `channels.json` are ingested; private channels, DMs, and group DMs are
skipped (reported in the parse summary log).

## Testing

```bash
uv run pytest tests/
```

Tests generate export archives in `tmp_path` (no checked-in fixtures needed) and include an
offline end-to-end run driving the source through a real `dlt` replace to prove a message dropped
between snapshots is physically removed — exactly what cognee's `orphan_cleanup` reconciles
against.
