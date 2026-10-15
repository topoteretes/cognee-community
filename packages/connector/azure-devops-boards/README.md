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
import cognee
from cognee_community_connector_azure_devops_boards import azure_devops_boards_source

await cognee.remember(
    azure_devops_boards_source(organization="my-org", project="my-project"),
    dataset_name="my_backlog",
    write_disposition="merge",
)

answer = await cognee.search(
    query_text="Which open bugs are blocking the payments release?",
    datasets=["my_backlog"],
)
```

Always pass `write_disposition="merge"`. cognee's default for this call is `"replace"`, and the connector only sends what changed since the last run, so under replace every unchanged work item would be forgotten.

Run the same call again later to pick up changes. Each project keeps its own cursor, so you can sync several projects, into one dataset or separate ones, without them stepping on each other. Keep connector data out of datasets you fill by hand, though, so cleanup only ever touches what the connector put there.

### Options

| Argument | What it does |
|---|---|
| `organization` | Organisation name from `https://dev.azure.com/<organization>` |
| `project` | Project name or id |
| `pat` | Token. Falls back to `AZURE_DEVOPS_PAT` |
| `organization_url` | Full URL instead of `organization`, for Azure DevOps Server or `*.visualstudio.com` |
| `area_paths` | Only sync work items under these area paths, e.g. `["Shop\\Payments"]` |
| `work_item_types` | Only sync these types, e.g. `["Bug", "User Story"]` |
| `include_comments` | Add the comment thread to each work item (default `True`) |

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

Changes come from the reporting revisions API (`_apis/wit/reporting/workitemrevisions`). It hands back a `continuationToken`, which is a server-side watermark, and the connector stores it as the cursor. So there's no clock to drift, and none of WIQL's 20,000 result limit. Adding a comment writes a new revision, which means new comments get picked up too.

For each changed item the connector fetches the full work item with `workitemsbatch`, 200 at a time, relations included.

Deleted work items (the recycle bin) show up in the same revisions feed and are sent as `_deleted` tombstones. dlt drops them on merge, and cognee's orphan cleanup removes them from the graph and vector stores. An item that moves out of the area paths or types you picked is removed the same way.

Destroyed items, the ones permanently deleted from the recycle bin, never appear in the feed again. To catch them, every run also does a cheap id-only WIQL sweep and tombstones whatever is gone. If that sweep comes back empty while items were known, the connector deletes nothing. An empty project is far less likely than a failed listing, and forgetting everything is the worse mistake.

Requests honour `Retry-After`, including when Azure DevOps sends it on a normal 200 as an early warning before it starts slowing you down.

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
