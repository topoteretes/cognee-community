# cognee-community-connector-azure-devops-boards

Syncs Azure DevOps Boards work items into [cognee](https://github.com/topoteretes/cognee), so an agent can answer questions about your backlog, like why a bug got closed or which pull request fixed it.

Each work item becomes one document with its fields, links, description and comment thread. It goes through normal cognify, so people, components and decisions mentioned in it end up in the graph. Syncs are incremental, and work items deleted in Azure DevOps are removed from memory on the next run.

## Install

```bash
pip install cognee-community-connector-azure-devops-boards
# or, from this repo:
cd packages/connector/azure-devops-boards && uv sync
```

## Create a token

1. In Azure DevOps, open User settings > Personal access tokens > New Token.
2. Pick the organisation, set an expiry, and choose Custom defined scopes.
3. Tick Work Items: Read. That's all the connector needs.
4. If you want, also tick Project and Team: Read. Each team has its own board, and with this scope the connector labels every board column with the team and board it belongs to. Without it you still get the column values, just unlabelled.

```bash
export AZURE_DEVOPS_PAT="..."
export LLM_API_KEY="..."   # cognee's LLM key, as for any cognee run
```

## Usage

```python
import os

import cognee
from cognee_community_connector_azure_devops_boards import azure_devops_boards_source

source = azure_devops_boards_source(
    organization="my-org",
    project="my-project",
    pat=os.environ["AZURE_DEVOPS_PAT"],
)
await cognee.remember(source, dataset_name="my_backlog", write_disposition="merge")
print(source.cognee_sync_stats)  # {"scanned": ..., "skipped": ..., "deleted": ..., "failed": ...}

answer = await cognee.search(
    query_text="Which open bugs are blocking the payments release?",
    datasets=["my_backlog"],
)
```

Always pass `write_disposition="merge"`. cognee's default for this call is `"replace"`, and the connector only sends what changed since the last run, so under replace every unchanged work item would be forgotten.

Run the same call again later to pick up changes. Each project keeps its own cursor, so you can sync several projects, into one dataset or separate ones, without them stepping on each other. If you sync the same project twice with different selections, give each source its own `resource_name`. With the default name, a run refuses state that was written under a different selection, so two sources can't quietly overwrite each other. To change the selection of an existing source, pass `resource_name` explicitly with the same name as before (`azure_devops_<org>_<project>` by default). Keep connector data out of datasets you fill by hand, though, so cleanup only ever touches what the connector put there.

### Options

| Argument | What it does |
|---|---|
| `project` | Project name or id |
| `pat` | Personal access token. Passed in, never read from the environment by the connector |
| `organization` | Organisation name from `https://dev.azure.com/<organization>` |
| `organization_url` | Full URL instead of `organization`, for Azure DevOps Server or `*.visualstudio.com` |
| `area_paths` | Only sync work items under these area paths, e.g. `["Shop\\Payments"]` |
| `work_item_types` | Only sync these types, e.g. `["Bug", "User Story"]` |
| `include_comments` | Add the comment thread to each work item (default `True`) |
| `resource_name` | Name of the table and sync state. Defaults to `azure_devops_<org>_<project>` |
| `max_requests` | Request budget per run (default 5,000, at least 1). A big first sync finishes over several runs |
| `max_wait_seconds` | Time one run may spend sleeping on `Retry-After` and backoff (default 900) |
| `check_active` | Optional callback run before every request, for hosts that need to stop a sync |

You don't need `max_rows_per_table=0`. cognee already reads document sources back in full when it decides what to forget (`resolve_dlt_sources` passes `0` for them), so orphan cleanup always compares against every stored work item.

## What a work item looks like in memory

```
Bug #412: Card payments fail on Safari 17

Type: Bug
State: Active
Board column: Doing (Payments team / Stories)
Assigned to: Priya Nair
Area: Shop\Payments
Iteration: Shop\Sprint 14
Priority: 1
Parent: work item #398
Pull requests: pull request !87

Repro steps:
Open checkout on Safari 17 and pay with a saved card...

Comments:
- Ravi Kumar on 2026-10-02: Only happens when 3DS is triggered.
```

Work items are written as `work item #123` and pull requests as `pull request !45`, the way Azure DevOps refers to them. If you also sync the matching repos, both connectors name the same pull request the same way and the graph links them.

Email addresses are never ingested, only display names.

## How sync and deletion work

Changes come from the reporting revisions API (`_apis/wit/reporting/workitemrevisions`). It hands back a `continuationToken`, which is a server-side watermark, and the connector stores it as the cursor. So there's no clock to drift, and none of WIQL's 20,000 result limit. Adding a comment writes a new revision, which means new comments get picked up too. If the feed ever hands back the same token without finishing, the run fails rather than saving a cursor that skips changes.

For each changed item the connector fetches the full work item with `workitemsbatch`, 200 at a time, relations included.

A work item leaves memory in one of three ways, each sent as a `_deleted` tombstone that dlt drops on merge and cognee's orphan cleanup removes from the graph and vector stores:

- the revisions feed marks it deleted (it went to the recycle bin)
- it moved out of the selected area paths or types
- it's gone from the id-only WIQL sweep that runs at the end of every run, which is how items destroyed from the recycle bin are caught

The batch call can also leave an item out for other reasons, like a per-item error. That alone never deletes anything: the item keeps its document and the sweep decides. If the sweep comes back empty while items were known, nothing is deleted, because a failed listing is far more likely than an empty project.

Changing `area_paths`, `work_item_types` or `include_comments` re-reads the whole project once, so newly selected items come in and deselected ones are forgotten.

Each run stops after `max_requests` requests, after `max_wait_seconds` of waiting, or when Azure DevOps keeps answering 429 or 5xx after retries. It stops cleanly: whatever was synced stays, the work items it didn't get to are kept in state, and the next run starts with those. `cognee_sync_stats` then has `failed: 1` plus one of `failed_budget`, `failed_slow_down`, `failed_rate_limit` or `failed_server_error`.

Requests honour `Retry-After`, including when Azure DevOps sends it on a normal 200 as an early warning, capped at 60 seconds per wait and at `max_wait_seconds` per run. Errors are raised as `AzureDevOpsAuthError` (401, or a 203 with the HTML sign-in page that a bad token can get), `AzureDevOpsAccessError` (403) or `AzureDevOpsError`, with the status in the message and the URL only on the exception's `url` attribute. An `organization_url` without `https://` is rejected straight away instead of being retried.

Board column labels are cached in state and refreshed at most once a day, so a normal run makes no extra requests for them. A refresh costs one request per team plus two per board and doesn't count against `max_requests`. If the token lacks Project and Team (Read), the cached labels are kept, or none if there never were any. Any other error on those calls fails the run, including a 404 from a single team's boards, because rendering the columns without labels would change the text of every work item on that board.

Editing or deleting an existing comment may not write a new revision. When it doesn't, the change shows up the next time the work item itself is updated.

## Testing

```bash
uv run pytest tests/
```

The unit tests fake the Azure DevOps API, so they need no account. One extra test runs against a real organisation when these are set:

```bash
export AZURE_DEVOPS_PAT="..."
export AZURE_DEVOPS_ORG="my-org"
export AZURE_DEVOPS_PROJECT="my-project"
uv run pytest tests/test_live.py
```
