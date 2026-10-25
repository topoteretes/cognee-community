# cognee-community-connector-terraform-cloud

A Terraform Cloud (HCP Terraform) data-source connector for
[cognee](https://github.com/topoteretes/cognee): turn your workspaces and run history into
memory, so you can ask "what changed in networking-prod this week, and why did the last
apply fail?".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Every workspace becomes one
document and every finished run one more (status, trigger, commit, plan and apply counts,
the planned resource changes and diagnostics), all flowing through cognee's normal cognify
entity extraction. Every text field is scrubbed for secrets before it is ingested.

## Install

```bash
uv pip install cognee-community-connector-terraform-cloud
# or, from this monorepo:
cd packages/connector/terraform-cloud && uv sync
```

## Setup

1. Create a token. A **team token** whose team can read runs on the workspaces you want is
   enough (Organization settings > API tokens > Team Tokens). User tokens also work.
2. Pass it as `token=` with the organization name. The example reads both from the
   environment, together with your `LLM_API_KEY` like any other cognee run:

```bash
export TFE_TOKEN="..."
export TFE_ORGANIZATION="acme"
# HCP Europe or Terraform Enterprise only (passed as hostname=):
# export TFE_HOSTNAME="app.eu.terraform.io"
```

## Usage

```python
import os

import cognee
from cognee_community_connector_terraform_cloud import terraform_cloud_source

await cognee.remember(
    terraform_cloud_source(os.environ["TFE_ORGANIZATION"], token=os.environ["TFE_TOKEN"]),
    dataset_name="terraform",
    primary_key="id",
    write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
    max_rows_per_table=0,
    self_improvement=False,
)

answer = await cognee.recall("Why did the last networking apply fail?", datasets=["terraform"])
```

Run the same call again to sync only what changed. See `examples/example.py`.

### Choosing what to ingest

| Argument | Default | Meaning |
| --- | --- | --- |
| `workspace_names` | all | Exact workspace names to sync. A name that was never synced and is not in the organization raises. |
| `workspace_wildcard` | none | Name pattern with `*`, for example `"*-prod"`. |
| `workspace_tags` | none | Only workspaces with all of these tags. |
| `project_id` | none | Only workspaces of this project. |
| `include_speculative` | `False` | Also ingest plan-only (speculative) runs, such as pull request plans. |
| `include_plan_output` | `True` | Add the plan summary to each run. |
| `since_days` | `90` | How far back the first sync reads runs. |
| `hostname` | `app.terraform.io` | Or `app.eu.terraform.io`, or a Terraform Enterprise host. |
| `max_requests` | `1000` | Request budget of one run. A bigger organization finishes over several runs. |
| `resource_name` | `terraform_cloud_<organization>` | Staging table, which scopes this sync's state and cleanup (the host is added when it is not the default). Use a different name per sync scope that shares a dataset. |

## Plan output and the scrubbing step

Plans can contain secrets: a value that is not marked `sensitive` is printed in clear text.
So the connector keeps a **summary**, never the full plan:

```
Planned changes:
- aws_s3_bucket.logs will be created
- aws_db_instance.main must be replaced
Plan: 1 to add, 0 to change, 1 to destroy.
Diagnostics:
Error: Invalid value for variable
```

Resource addresses with their action, the `Plan:` line and diagnostic summaries are kept;
attribute values are not read at all. Both plain text logs and structured run output are
understood. The apply log is only read when an apply failed.

On top of that, every text field (run messages, commit messages, workspace descriptions,
the plan summary) goes through `redact.scrub` before it is emitted. It strips color codes
first, then replaces private keys, AWS access keys, GitHub, GitLab, Slack and HCP Terraform
tokens, JWTs, Azure account keys, credentials inside URLs and secret-looking `key = value`
pairs with `[REDACTED:<kind>]`. The number of redactions is reported in
`cognee_sync_stats["redacted"]`. Pattern matching lowers the risk, it cannot promise to
catch every format, which is why values are left out in the first place.

Plan logs are read from HCP Terraform's blob storage through a one-time URL. That URL is a
secret: it is used once and never logged, stored or ingested, and the API token is never
sent to it.

## How sync and forget-on-delete work

- **Cursor.** The run list endpoints allow only 30 requests per minute and have no
  `created-at` filter. So runs are listed for the whole organization (or the selected
  workspaces), 100 per page, and paging stops once runs are older than the newest
  `created-at` seen last time. It stops early only while the API keeps answering newest
  first; otherwise it reads on.
- **Finished runs only.** A run is ingested once it reaches a final state (`applied`,
  `planned_and_finished`, `errored`, `discarded`, `canceled`, ...). Runs still in progress
  are kept in state and checked again on the next sync, so one run is never processed
  once per status. A run waiting for confirmation appears once it is applied or discarded.
  The organization run listing cannot include a run's plan and apply (it answers HTTP 400
  for them), so each new finished run is read once on its own, which brings them along.
- **Workspaces** are listed on every sync. Their documents leave out fields that change on
  every run (resource count, lock state), so they are only processed again when the
  workspace itself changes.
- **Forget-on-delete.** Runs cannot be deleted on their own; they go away with their
  workspace. A workspace that is deleted, no longer visible to the token, or no longer
  selected is removed from memory on the next sync, with all of its runs. This is decided
  right after the workspace listing, so a run cut short still forgets them. A workspace
  listing that comes back empty while workspaces were synced before forgets nothing until
  the next run confirms it, and an answer without its `data` list raises.
- **A run is bounded.** 429 and 5xx are retried, honoring `Retry-After`. A `Retry-After`
  over 60 seconds, a 429 that outlasts the retries, or more than five minutes of retry
  waits stops the run cleanly with `failed` and `failed_rate_limit`; a spent
  `max_requests` budget is a planned pause and sets only `failed_budget`. The runs
  finished so far are kept and the cursor stays where it was, so the next run picks up the
  rest. Any other error raises, so dlt keeps the previous state.

`cognee_sync_stats` counts `emitted` and `deleted` rows, `pending` runs and `redacted`
secrets, and `failed` plus `failed_*` for a stopped run.

## Privacy

Plans and logs describe your infrastructure. Nothing is fetched until you call
`remember`, and anyone with read access to the target dataset can read what was ingested,
so keep Terraform in its own dataset. Workspace variables are never read. The token stays
in memory and is never written into documents, state or error messages.

## Testing

```bash
uv run pytest tests/
```

The tests need no HCP Terraform account. They cover the scrubbing step on every secret
kind, the client against a mocked HTTP transport (no token sent to blob storage), log
summaries for both log formats, retries and their limits, the sync against a fake API
(cursor and early stop, runs in progress, speculative runs, deletions, empty answers,
unknown workspace names, rate limit and budget stops, `check_active`) and through a real
dlt pipeline (two sources in one dataset, failed runs), and `cognee.remember` end to end
to prove a deleted workspace leaves the graph.
