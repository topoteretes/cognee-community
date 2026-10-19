# cognee-community-connector-attio

An [Attio](https://attio.com) data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync your CRM into memory — "ask my Attio".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Records, notes
and (optionally) list entries are rendered to short text documents and ingested as **normal
documents**: they flow through cognee's cognify entity-extraction pipeline, not the
deterministic dlt-row path, via cognee's document-mode marker.

It targets the [Attio REST API v2](https://docs.attio.com/rest-api/overview) and authenticates
with an access token.

## What it ingests

| What | Source endpoint | Row id |
|---|---|---|
| Records of every object (people, companies, deals, **custom objects**) | `POST /v2/objects/{object}/records/query` | `<object>:<record_id>` |
| Notes attached to those records | `GET /v2/notes` | `note:<note_id>` |
| List entries (opt-in) | `POST /v2/lists/{list}/entries/query` | `entry:<list>:<entry_id>` |

Attio's object model is custom per workspace, so nothing is hardcoded: the connector reads the
objects and each object's attributes at runtime and renders every attribute that has a value as
a `Title: value` line. A record's name comes from its `name` attribute, falling back to a
personal-name or the first text attribute. References to other records (a deal's company) are
rendered as that record's name. Actor references (a bare workspace-member id) and interactions
(derived data) are left out, as are archived attributes.

## Install

```bash
uv pip install cognee-community-connector-attio
# or, from this monorepo:
cd packages/connector/attio && uv sync
```

## Setup

1. In Attio open **Workspace settings → Developers → + New access token**.
2. Give it read-only scopes: `object_configuration:read`, `record_permission:read` and
   `note:read` (add `list_configuration:read` and `list_entry:read` if you ingest lists).
3. Set the token as `ATTIO_API_KEY` (or pass `api_key=...`), plus your `LLM_API_KEY` like any
   other cognee run.

A free Attio workspace is enough to try it.

## Usage

```python
import cognee
from cognee_community_connector_attio import attio_source

await cognee.remember(
    attio_source(),  # ATTIO_API_KEY from env, or pass api_key=...
    dataset_name="attio",
)

answer = await cognee.search(
    query_text="What did we discuss with Acme?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["attio"],
)
```

| Argument | Default | Meaning |
|---|---|---|
| `api_key` | `ATTIO_API_KEY` | Attio access token |
| `objects` | all objects | Object slugs to ingest, e.g. `["people", "companies"]` |
| `lists` | none | List slugs whose entries to ingest |
| `include_notes` | `True` | Also ingest notes on the selected objects' records |

An unknown object or list slug raises a `ValueError` that names the slugs the workspace has.
See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

The source is a **full snapshot**: `write_disposition="replace"` rewrites staging with exactly
the rows currently visible to the token on each run. Attio has no delete feed, and the
documented `records/query` filters cover record attributes rather than a modification time, so a
cursor could not see deletions. A record, note or entry deleted in Attio simply stops being
listed, drops out of the snapshot, and cognee's existing `orphan_cleanup` removes it from the
graph and vector stores. Rows are rendered deterministically from first-party fields only (no
timestamps or counters), so unchanged rows keep a stable content-hash `data_id` and are not
re-ingested or re-cognified. A failing API call aborts the run, leaving memory untouched, rather
than letting a partial snapshot forget live rows.

Things to know:

- A run reads every selected object in full before rendering, because a reference renders as
  the target's name. Use `objects=[...]` to keep large workspaces in scope.
- A reference to a record outside the selected objects is left out rather than shown as an id.
- Rate limits (HTTP 429, honouring `Retry-After`), 5xx and network errors are retried with
  backoff; a missing scope (HTTP 403) fails with a message naming the endpoint, so grant the
  scopes above or switch notes/lists off.
- Error messages never contain the token or workspace-specific slugs.

## Testing

```bash
uv run pytest tests/
```

The tests mock the Attio API (no live token) and cover attribute rendering, pagination
(including a server that caps the page size), the client's retry and error handling, and the
sync model: a rerun of an unchanged workspace is a no-op, an edit replaces the row, and a
record deleted upstream is forgotten — through a dlt pipeline and end to end through cognee's
`add` / `cognify` with the LLM mocked.
