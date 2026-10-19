# cognee-community-connector-vercel

A Vercel data-source connector for [cognee](https://github.com/topoteretes/cognee). It syncs
projects, deployments and the build output of failed deployments into memory, so an agent can
answer "which deploy broke, on which commit, and what did the build say".

It exposes a `dlt` source, built on dlt's declarative `rest_api` source, that you hand to
`cognee.remember(...)` / `cognee.add(...)`. Rows are ingested as normal documents through
cognee's document-mode marker.

## Install

```bash
uv pip install cognee-community-connector-vercel
# or, from this monorepo:
cd packages/connector/vercel && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_vercel import vercel_source

await cognee.remember(
    vercel_source(),  # VERCEL_TOKEN from env, or pass token=...
    dataset_name="vercel",  # give the connector its own dataset
    self_improvement=False,  # skip the enrichment pass on repeated syncs
)

answer = await cognee.search(
    query_text="Which deployments failed this week, and why?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["vercel"],
)
```

| Argument | Default | What it does |
|---|---|---|
| `token` | `VERCEL_TOKEN` | Access token, sent as a bearer token. |
| `team_id` | `VERCEL_TEAM_ID` | Team to read. Needed when a full-account token should read a team. |
| `project_ids` | all projects | Restrict the sync to these project ids or names (any case). One entry, or 2 to 20 `prj_` ids, is filtered by Vercel itself. A listed project that no longer exists is treated as deleted. |
| `lookback_days` | `30` | How far back deployments are read, in days (more than 0). `None` reads all of them. |
| `include_build_logs` | `True` | Fetch build output for failed deployments. |
| `allow_empty` | `False` | Let a sync through when the account lists no projects at all although an earlier sync listed some. |
| `resource_name` | `vercel`, or `vercel_<team id>` | Prefix of the staging tables. Each team gets its own by default. |
| `check_active` | none | Called before every page is used. Raise from it to stop a sync whose access was revoked. |
| `max_log_chars` | `20000` | Keep only the end of a build log. Only the newest `max_log_chars // 10` lines (at least 100) are requested. `None` requests and keeps all of it. |

See `examples/example.py` for the full flow.

## What is ingested

| Staging table | One document per | Source |
|---|---|---|
| `vercel_projects` | project | `GET /v10/projects` |
| `vercel_deployments` | deployment: state, target, timing, author, commit, error | `GET /v7/deployments` |
| `vercel_build_logs` | failed deployment that has build output | `GET /v3/deployments/{id}/events` |

Build output is requested only for deployments in state `ERROR`.

Environment variables are never ingested. The project object Vercel returns embeds `env`
entries with their values, deploy-hook URLs and protection-bypass data, so every row is rebuilt
from a fixed list of fields before dlt writes anything. The env endpoints are never called.

## How sync and forget-on-delete work

Each run is a **full snapshot of a fixed window**: every deployment created in the last
`lookback_days`, loaded with `write_disposition="replace"`.

- Unchanged rows keep a stable content-hash id, so they are not re-ingested or re-cognified.
- A deployment that moved from `BUILDING` to `ERROR` since the last run is re-read and updated.
  A `since` cursor would miss it, because `since` filters on creation time.
- A deployment that was deleted, removed by Vercel's retention policy, or is now older than the
  window drops out of the snapshot, and cognee's `orphan_cleanup` forgets it.
- An HTTP error or an unexpected response shape aborts the run and leaves memory untouched: a
  list without its `pagination` block, or build events that are not a list, count as unexpected.
  A partial snapshot would otherwise be forgotten as if it were a deletion.
- An account that suddenly lists no projects at all, after an earlier sync listed some, aborts the
  run too. A token for the wrong team looks exactly like that. Pass `allow_empty=True` once to
  forget an account that really is empty.

Leave `write_disposition` at its default. Passing `merge` breaks forget-on-delete for a snapshot
source.

## Limits

- **The window bounds memory.** Anything older than `lookback_days` is forgotten on the next
  sync. Keep it at or below your plan's deployment retention.
- **Build logs are ingested as Vercel returns them.** Vercel redacts sensitive environment
  variables of 32 characters or more. Anything else a build prints stays in its log. Pass
  `include_build_logs=False` if your builds print secrets.
- **Teams can share a dataset, selections can't.** Each team gets its own tables by default
  (`vercel_<team id>_projects` and so on), so syncing two teams into one dataset keeps both. Two
  different `project_ids` selections of one team share tables, so give each its own
  `resource_name` or its own dataset, or each sync forgets the other's documents.
- **Projects page by last update.** With more than 100 projects, a project updated while a sync is
  paging can be missed by that one sync. It comes back, and is re-processed, on the next.
- **dlt keeps its load files.** Each sync leaves a gzip copy of the loaded rows in dlt's
  pipeline directory, and forgetting a document does not remove those copies. Set
  `LOAD__DELETE_COMPLETED_JOBS=true` to have dlt delete them after each load.
- **Auth is a token.** It is sent as a bearer token. That is also how a token from an
  integration install is used, but only a personal token was tested. The connector does not run
  an OAuth flow itself.
- **Who can read it.** Build logs, commit messages and author names land in the dataset. Anyone
  with read access to the dataset in cognee can read them, so keep it private if builds can print
  secrets.
- Tested live against one personal Vercel account in October 2026.

## Setup

1. Create an access token at <https://vercel.com/account/tokens>. Scope it to one team and give
   it an expiry.
2. Set it as `VERCEL_TOKEN` (or pass `token=...`), plus your `LLM_API_KEY` like any other cognee
   run.

## Testing

```bash
uv run --with pytest pytest tests/
```

The tests run offline. A fake `requests` transport stands in for api.vercel.com, so the real
`rest_api` source, paginator and auth run against in-memory fixtures. They cover the field
allowlist (no env value, hook URL or bypass secret reaches staging), pagination, failed-only log
requests, state changes and deletions on re-sync, and aborting on a failed or malformed read. One
test runs `cognee.add()` and `cognify()` with the LLM and embeddings mocked and checks that a
deleted deployment leaves the graph.
