# cognee-community-connector-azure-repos

Syncs Azure Repos pull requests into [cognee](https://github.com/topoteretes/cognee), so an agent can answer the questions that usually mean digging through old PRs, like why a change was made or who pushed back on it.

Each pull request becomes one document with its description, reviewers and their votes, linked work items, commit messages and review threads. It goes through normal cognify. Later runs only fetch PRs that changed, and PRs from a repo you delete or stop syncing are removed from memory.

## Install

```bash
pip install cognee-community-connector-azure-repos
# or, from this repo:
cd packages/connector/azure-repos && uv sync
```

## Create a token

In Azure DevOps, open User settings > Personal access tokens > New Token, choose Custom defined, and tick Code: Read. That's the only scope it needs. If you also use the Azure DevOps Boards connector, one token with Code: Read and Work Items: Read covers both.

```bash
export AZURE_DEVOPS_PAT="..."
export LLM_API_KEY="..."   # cognee's LLM key, as for any cognee run
```

## Usage

```python
import os

import cognee
from cognee_community_connector_azure_repos import azure_repos_source

source = azure_repos_source(
    organization="my-org",
    project="my-project",
    pat=os.environ["AZURE_DEVOPS_PAT"],
)
await cognee.remember(source, dataset_name="my_repos", write_disposition="merge")
print(source.cognee_sync_stats)  # {"scanned": ..., "skipped": ..., "deleted": ..., "failed": ...}

answer = await cognee.search(
    query_text="Why did we switch the payment client to retries?",
    datasets=["my_repos"],
)
```

Always pass `write_disposition="merge"`. cognee's default for this call is `"replace"`, and later runs only send PRs that changed, so under replace every other PR would be forgotten.

Each project keeps its own state, so you can sync several projects into one dataset or separate ones. If you sync the same project twice with different selections, give each source its own `resource_name`. With the default name, a run refuses state that was written with different options, so two sources can't quietly overwrite each other. To change the options of an existing source, pass `resource_name` explicitly with the same name as before (`azure_repos_<org>_<project>` by default).

### Options

| Argument | What it does |
|---|---|
| `project` | Project name or id |
| `pat` | Personal access token. Passed in, never read from the environment by the connector |
| `organization` | Organisation name from `https://dev.azure.com/<organization>` |
| `organization_url` | Full URL instead of `organization`, for Azure DevOps Server or `*.visualstudio.com` |
| `repositories` | Repository names to sync. All of the project's by default |
| `include_abandoned` | Keep abandoned PRs (default `True`). They often explain why something wasn't done |
| `include_commits` | Add commit messages (default `True`) |
| `created_after` | Skip PRs created before this `date` |
| `recheck_closed_days` | Read the threads of PRs closed in the last N days on every run (default `30`). `None` reads every PR's threads every run |
| `resource_name` | Name of the table and sync state. Defaults to `azure_repos_<org>_<project>` |
| `max_requests` | Request budget per run (default 5,000, at least 1). A big first sync finishes over several runs |
| `max_wait_seconds` | Time one run may spend sleeping on `Retry-After` and backoff (default 900) |
| `check_active` | Optional callback run before every request, for hosts that need to stop a sync |

## What a pull request looks like in memory

```
Pull request !87: Retry card payments on timeout

Repository: shop-api
Status: merged into main from feature/payment-retry
Created by: Priya Nair
Reviewers: Meera Shah (waiting for author), Ravi Kumar (approved)
Work items: work item #412

Description:
Card payments time out on Safari when 3DS kicks in...

Commits:
- 1a2b3c4 Retry on timeout (Priya Nair)

Review discussion:
On src/pay.py line 42 (resolved):
- Meera Shah: Should this retry on 429 too?
- Priya Nair: Done, and it honours Retry-After.
```

Linked work items are written as `work item #412` and the PR itself as `pull request !87`, the same way the Azure DevOps Boards connector writes them, so if you sync both the graph joins a bug to the PR that fixed it.

System messages ("X voted 10", "the reference was updated") and deleted comments are left out. Email addresses are never ingested.

## How sync works

Pull requests have no "last updated" field and the API can't list PRs changed since a date, so there's no cursor to keep. Each run lists every PR in the selected repos, which costs about one request per 500 PRs, and compares a fingerprint of what the list shows: status, title, head commit, draft flag, labels, reviewer votes and the repo name.

A lot of what changes on a PR doesn't show up in that list: new comments, a work item getting linked, or an edit past the first 400 characters of the description. So for PRs that are still open, or were closed in the last `recheck_closed_days` days, the connector also reads the full PR and its review threads on every run, two requests each, and fingerprints those too. Only PRs where something moved get their commits read and a new document written.

The trade-off is that a comment left on a PR closed more than 30 days ago is picked up the next time that PR changes. If that matters to you, set `recheck_closed_days=None`, which costs two requests per PR per run.

Changing `include_commits` re-renders every PR once on the next run, since it changes the text without changing anything the fingerprints look at.

Each run stops after `max_requests` requests, after `max_wait_seconds` of waiting, or when Azure DevOps keeps answering 429 or 5xx after retries. It stops cleanly: PRs already synced keep their new fingerprints, nothing is deleted because the listing didn't finish, and the next run skips what's done and carries on. `cognee_sync_stats` then has `failed: 1` plus one of `failed_budget`, `failed_slow_down`, `failed_rate_limit` or `failed_server_error`.

Requests honour `Retry-After`, including when Azure DevOps sends it on a normal 200 as an early warning, capped at 60 seconds per wait and at `max_wait_seconds` per run. Errors are raised as `AzureDevOpsAuthError` (401, or a 203 with the HTML sign-in page that a bad token can get), `AzureDevOpsAccessError` (403) or `AzureDevOpsError`, with the status in the message and the URL only on the exception's `url` attribute. An `organization_url` without `https://` is rejected straight away instead of being retried. If a listing or commit page keeps coming back the same, the run fails instead of looping.

Some API details it handles for you. The PR list only returns active PRs unless asked for every status, it cuts descriptions at 400 characters, and long commit messages come back truncated. Each of these would silently lose content.

## Deletes

Azure DevOps doesn't let you delete a pull request, only abandon it. So what disappears is a repository, and its PRs are removed from memory when the repo is deleted, removed from `repositories`, or no longer visible to the token. With `include_abandoned=False`, a PR that gets abandoned is removed too.

A disabled repository keeps its PRs, since disabled isn't deleted. If the repo listing comes back empty while PRs were known, nothing is deleted, because a failed listing is far more likely than a project with no repos left.

Removed PRs are sent as `_deleted` tombstones. dlt drops them on merge and cognee's orphan cleanup removes them from the graph and vector stores.

## Testing

```bash
uv run pytest tests/
```

The unit tests fake the Azure DevOps API, so they need no account. `tests/test_live.py` is a read-only check against a real project and is skipped unless these are set:

```bash
export AZURE_DEVOPS_PAT="..."
export AZURE_DEVOPS_ORG="my-org"
export AZURE_DEVOPS_PROJECT="my-project"
```
