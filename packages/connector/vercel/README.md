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
| `project_ids` | all projects | A list of project ids or names (any case) to restrict the sync to. Names are turned into ids through the run's projects list, which is read once per run, and deployments are matched on the project id, so a renamed project keeps its older deployments. Up to 20 projects are filtered by Vercel itself. A listed project the token cannot see stops the run, first sync included, and the error names it. |
| `lookback_days` | `30` | How far back deployments are read, in days (more than 0). `None` reads all of them. |
| `include_build_logs` | `False` | Also fetch the build output of failed deployments. See Limits before switching it on. |
| `allow_empty` | `False` | Let one sync through that lists no projects or only another account's projects although an earlier sync found some, or that lacks entries of `project_ids`. Their documents are forgotten. Missing `project_ids` entries are remembered, so later runs skip them without the flag, until they are found again. Each run warns about them, with the time of the run that accepted them, and lists them in `cognee_sync_stats["skipped_projects"]`. |
| `resource_name` | `vercel`, or `vercel_<team id>` | Prefix of the staging tables. Each team gets its own by default. |
| `check_active` | none | Called before every page is used. Raise from it to stop a sync whose access was revoked. |
| `service` | dlt's session, kept short | `requests.Session` to send requests through. By default a 429 stops the run with memory unchanged, because waiting would hold cognee's staging lock and the next run re-reads everything. A 5xx or a dropped connection gets one retry after at most 5 s. Each request times out after 30 s. `session` is accepted as another name for it. |
| `max_log_chars` | `20000` | Keep only the end of a build log. Only the newest `max_log_chars // 10` lines (at least 100) are requested. `None` requests and keeps all of it. |

All arguments are keyword-only. After a run, `source.cognee_sync_stats` holds the row count of
each table (`projects`, `deployments`, `build_logs`) and `failed_rate_limit`, the number of 429
answers that stopped the run, plus `skipped_projects`, the `project_ids` entries a run skipped
because an earlier `allow_empty=True` run accepted them as gone. The counts are cleared when a run
starts and logged when it ends. A table that empties after holding rows logs a warning.
`with_resources()` and `parallelize()` are not supported: the deployment tables use the projects
list that the projects table reads in the same run.

See `examples/example.py` for the full flow.

## What is ingested

| Staging table | One document per | Source |
|---|---|---|
| `<resource_name>_projects` | project | `GET /v10/projects` |
| `<resource_name>_deployments` | deployment: state, target, timing, author, commit, error | `GET /v7/deployments` |
| `<resource_name>_build_logs` | failed deployment that has build output | `GET /v3/deployments/{id}/events` |

`resource_name` is `vercel` without a team, so the tables are `vercel_projects` and so on.

Build output is requested only with `include_build_logs=True`, and only for deployments in state
`ERROR`.

The project object Vercel returns embeds `env` entries with their values, deploy-hook URLs and
protection-bypass data. Every row is rebuilt from a fixed list of fields before dlt writes
anything, so none of those are copied, and the env endpoints are never called. A build log is
different: it holds whatever the build printed, env values included (see Limits).

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
  A partial snapshot would otherwise be forgotten as if it were a deletion. The one exception is
  the events call: 404 or 410 there means the build has no output to read (see Limits).
- Pages overlap by one millisecond. Vercel's `until` cursor is exclusive and points at the last
  item of the page, so a second deployment from that same millisecond would fall between two
  pages. The repeated item is dropped by id. If a cursor stops moving, the run aborts.
- An account that suddenly lists no projects at all, after an earlier sync listed some, aborts the
  run too, and so does one whose projects all belong to another account (each project carries its
  `accountId`). A token for the wrong team looks like either. Pass `allow_empty=True` once to
  forget an account that really is empty, or to replace one account with another.

Leave `write_disposition` at its default. Passing `merge` breaks forget-on-delete for a snapshot
source.

## Limits

- **The window bounds memory.** Anything older than `lookback_days` is forgotten on the next
  sync. Keep it at or below your plan's deployment retention.
- **Build logs are ingested as Vercel returns them.** That is why they are off by default.
  Vercel redacts sensitive environment variables of 32 characters or more. Anything else a build
  prints, a shorter secret or a plain env value, stays in its log and lands in the dataset.
- **Build output expires.** When Vercel no longer keeps a failed build's output, the events call
  answers 410 or 404. While the deployment is still listed, its log row from the last load is
  read back from staging and loaded again unchanged, so the document stays until the deployment
  goes. A failed build whose log expired before its first sync has no log document; its
  deployment document still carries the error.
- **Teams can share a dataset, selections can't.** Each team gets its own tables by default
  (`vercel_<team id>_projects` and so on), so syncing two teams into one dataset keeps both. Two
  different `project_ids` selections of one team share tables, so give each its own
  `resource_name` or its own dataset, or each sync forgets the other's documents.
- **Projects page by last update.** A project edited while the list is paged moves to the front,
  behind the cursor. The newest page is read again at the end of the list to pick those up, so
  only a sync during which more than 100 projects are edited can miss one, until the next sync.
- **Rate limits restart the run.** A run that hits Vercel's rate limit stops and the next run
  starts from the first page again; there is no resume. A run costs one request per 100 projects
  plus one, one per 100 deployments in the window, and with build logs one per 100 failed
  deployments plus one per failed build. If that is more than the limit allows, narrow
  `project_ids` or `lookback_days`.
- **Renaming leaves the old documents.** Changing `resource_name` or `team_id`, or switching
  `include_build_logs` off, stops writing to the old tables but does not forget their documents.
  To start clean, run `await cognee.forget(dataset="<your dataset>")` before the next sync.
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

## If you synced with an earlier version

Nothing has been released yet, but if you ran this connector from an earlier commit of its PR:

- Build logs are now off by default. Logs loaded by an earlier sync stay in the dataset, secrets
  included, until you run `await cognee.forget(dataset="<your dataset>")`.
- Tables are now named per team (`vercel_<team id>_*`), so an earlier sync's `vercel_*`
  documents stay next to the new ones until that same `forget()`.
- `resource_name` is checked more strictly: `__` and a trailing `_` are rejected with a
  `ValueError`. Pick a name of lowercase words joined by single underscores.

## Setup

1. Create an access token at <https://vercel.com/account/tokens>. Scope it to one team and give
   it an expiry.
2. Set it as `VERCEL_TOKEN` (or pass `token=...`), plus your `LLM_API_KEY` like any other cognee
   run.

## Testing

```bash
uv run --with pytest pytest tests/
```

CI (`.github/workflows/vercel_connector_tests.yml`) runs the suite on the versions in `uv.lock`
with Python 3.10 and 3.14, and on the floor in `pyproject.toml` (cognee 1.6.1, dlt 1.9.0).

The tests run offline. A fake `requests` transport stands in for api.vercel.com, so the real
`rest_api` source, paginator and auth run against in-memory fixtures. They cover the field
allowlist (no env value, hook URL or bypass secret reaches staging), pagination, failed-only log
requests, state changes and deletions on re-sync, and aborting on a failed or malformed read. Three
tests go through `cognee.add()`: two teams in one dataset, an expired build log keeping its
document, and one that also runs `cognify()` with the LLM and embeddings mocked and checks that
a deleted deployment leaves the graph.
