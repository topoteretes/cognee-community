# cognee-community-connector-circleci

A CircleCI data-source connector for [cognee](https://github.com/topoteretes/cognee): sync pipeline, workflow and job outcomes into memory, so an agent can answer "what broke on main last night, and why?". It syncs incrementally, picks up reruns, and forgets projects you remove from the sync.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each pipeline becomes one **document** that goes through cognee's normal cognify pipeline. Failed jobs carry their failing tests; build logs are never read, because they are large and mostly noise.

## Requirements

- cognee 1.6.1 or later (below 2): the first release with table-scoped document cleanup (`DOCUMENT_SYNC_VERSION >= 1`) and per-source pipeline state (`PIPELINE_SCOPE_ATTR`). The source refuses to run on an older cognee.
- Python 3.10 to 3.13.

## Install

```bash
uv pip install cognee-community-connector-circleci
# or, from this monorepo:
cd packages/connector/circleci && uv sync
```

## Usage

```python
import os

import cognee
from cognee_community_connector_circleci import circleci_source

await cognee.remember(
    circleci_source(
        project_slugs=["gh/acme/api"],
        token=os.environ["CIRCLECI_TOKEN"],
        resource_name="circleci_acme",  # one per source in a dataset, see below
    ),
    dataset_name="circleci",
    primary_key="id",
    write_disposition="merge",  # required: "replace" would forget every pipeline not new this run
    max_rows_per_table=0,  # lift the default 50-row cap
)

answer = await cognee.search(
    query_text="Which tests failed most recently, and why?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["circleci"],
)
```

| Argument | Default | What it does |
|---|---|---|
| `project_slugs` | required | Projects to sync: `gh/<org>/<repo>`, `bb/<workspace>/<repo>`, or `circleci/<org-id>/<project-id>` for GitHub App projects |
| `token` | required | CircleCI personal API token. The connector never reads environment variables; the example reads `CIRCLECI_TOKEN` and passes it in. |
| `resource_name` | `circleci_pipelines` | Name of the dlt resource, its staging table and its sync state. **Every CircleCI source syncing into one dataset needs its own.** Two sources sharing a name share state, so each sees the other's projects as dropped and forgets them. Renaming a source makes it start over, and documents synced under the old name are not cleaned up. |
| `branch` | all branches | Only sync pipelines on this branch. Changing it re-lists each project from the newest pipeline. |
| `max_initial_pipelines` | `50` | Pipelines ingested on a project's first sync, most recent first. `None` ingests the whole history. Later syncs take everything since the last one. |
| `pending_timeout_days` | `7` | Stop re-checking a pipeline that is still unfinished after this long (e.g. an approval nobody clicks) |
| `rerun_window_days` | `7` | Watch finished pipelines up to this many days old for reruns. Costs one request per such pipeline per sync. `0` turns it off. |
| `max_failing_tests` | `20` | Failing tests kept per failed job |
| `max_message_chars` | `500` | Characters kept from the **end** of each failure message, where the error and its location are |
| `base_url` | `https://circleci.com/api/v2` | API base URL. Pipeline links in the documents always point at `app.circleci.com`. |
| `check_active` | none | Optional callback for hosts, called before every request and every row. Raise from it to stop a sync whose connection was revoked. |
| `service` | none | A pre-built `requests`-like session, used instead of building one from the token. `session` is accepted as its old name. |

After a run, the returned resource's `cognee_sync_stats` holds integer counts: `scanned`, `skipped`, `deleted`, `failed` and `failed_rate_limit`. They are counts only, never slugs or failure text.

See `examples/example.py` for the full flow.

## What gets ingested

One document per pipeline, keyed by the pipeline id:

```
# gh/acme/api pipeline #1423 on main: failed

Project: gh/acme/api
Pipeline #1423, created 2026-10-07 21:32 UTC
Trigger: webhook by alice
Branch: main
Commit: f154c09 Fix retry on 429

Workflow build-and-test: failed
- deploy: not_run
- lint: success
- smoke: failed (no test results)
- test: failed
  Failing tests (2):
  - tests/test_client.py::test_retry_after
      def test_retry_after():
      >       assert client.calls == 3
      E       assert 1 == 3
      tests/test_client.py:42: AssertionError
  - ...
```

Passing jobs are a single status line, and test results are only fetched for failed jobs. Workflows, jobs and failing tests are sorted by name, and durations and timestamps other than the creation time are left out. A finished pipeline's text therefore never changes, so it is never re-cognified.

A rerun shows up as another run of the same workflow (`Workflow build-and-test (rerun 1): success`). The pipeline's status comes from the latest run of each workflow, so a failure that was rerun green reads as `success`, with the failed run kept as history.

## How sync + forget-on-delete work

- **Incremental sync.** Each project keeps a `created_at` cursor in dlt's per-resource state. Pipelines are listed newest first, and the listing stops at the cursor. A run with nothing new costs one listing request per project, plus re-checks of unfinished and recent pipelines. Rows are upserted by pipeline id with `write_disposition="merge"`.
- **Unfinished pipelines.** A pipeline still running at sync time is ingested and remembered in state. Later syncs re-check it and ingest it again only once it has finished, so its final status lands without re-ingesting every in-between state. "Finished" is decided from the workflows: a pipeline's own `state` stays `created` while they run.
- **Reruns.** Rerunning a workflow adds a new workflow to a pipeline that is already past the cursor. For `rerun_window_days`, each sync lists a finished pipeline's workflows and compares their ids with the last sync. If a workflow was added, the pipeline is fetched and ingested again.
- **Forget-on-delete.** CircleCI has no way to delete a single pipeline, so deletes happen per project. When a slug is dropped from `project_slugs`, every pipeline ingested for it is emitted with the `_deleted` hard-delete marker. dlt removes those rows on `merge`, and cognee's orphan cleanup purges them from the graph, vector and relational stores. That includes the last project of a dataset.
- **A 404 is an error, not a delete.** CircleCI answers 404 both for a deleted project and for one the token can no longer see, for example after the token was rotated to a narrower account or its user left the org. So a project whose listing returns 404 fails the run with `CircleCIProjectNotFoundError`, and its pipelines stay in memory. If the project really is gone, remove it from `project_slugs` to forget it.
- **Expired pipelines are kept.** A pipeline that ages out of CircleCI's retention stays in memory as history.
- **Other errors never delete.** An invalid token gets a 401 and fails the run (`CircleCIAuthError`) with memory untouched. Rate limits (429) and server errors are retried, honouring `Retry-After` up to 60 seconds, because the sync holds cognee's ingestion lock while it waits. Error messages carry the HTTP status only; the request path, which names the project, is on the exception's `path` attribute.

Sync each source with its full list of projects. A project left out of `project_slugs` is forgotten on that run.

## Privacy

- **Opt-in, read-only.** Nothing is synced until you call `circleci_source` with a token and a list of projects, and the connector only sends `GET` requests.
- **What is read:** for each listed project, its pipelines (number, branch or tag, commit SHA and subject, trigger type and actor login, config errors), their workflows and jobs (names and statuses), and the test results of failed jobs. Build logs, artifacts, environment variables and contexts are never read.
- **Who can see it:** the documents land in the cognee dataset you name. Anyone with access to that dataset can read the commit subjects, author logins and failure output in it, including from private repositories.
- **The token has more access than the connector uses.** CircleCI API v2 only accepts personal API tokens, and a personal token carries its user's full read *and write* access to every project they can see. CircleCI has no read-only token for API v2. Keep the token in a secret store, and consider a dedicated CircleCI user that only follows the projects you sync.
- **Failure output is stored.** Test failure messages are ingested as they are, so a test that prints a token or an environment value would put it in the graph. Before a message is stored, the connector masks the sync's own token and common credential shapes: `password=…`, `api_key: …` and similar key/value pairs, `Bearer`/`Basic` values, `user:password@` in URLs, and GitHub, GitLab, AWS, Slack, OpenAI-style, CircleCI and JWT tokens. That is a safety net, not a secret scanner, so it is still best not to print secrets in tests.

## Setup

1. Create a personal API token under **User Settings → Personal API Tokens** in CircleCI. It needs access to the projects you sync. See the privacy notes above on what such a token can do.
2. Find each project's slug under **Project Settings → Overview**.
3. For failing-test output, the project's `.circleci/config.yml` must save test results with a `store_test_results` step. Without it, a failed job is ingested as `failed (no test results)`.
4. Pass the token in, and set your `LLM_API_KEY` like any other cognee run:

   ```bash
   export CIRCLECI_TOKEN="..."
   export CIRCLECI_PROJECT_SLUG="gh/<org>/<repo>"
   export LLM_API_KEY="sk-..."
   uv run python examples/example.py
   ```

## Testing

```bash
uv run --with pytest python -m pytest -q
```

No live token is needed. The tests replay real CircleCI API responses recorded from a public fixture project (see `tests/fixtures/README.md`). Those cover failed, passing, on-hold and mid-run pipelines. The tests check:

- HTTP retries, the `Retry-After` cap, pagination that doesn't advance, and error types
- the document text, its ordering and redaction
- the cursor, branch changes, unfinished pipelines and reruns
- forget-on-delete, and that a 404 never deletes
- full runs through a dlt pipeline into a temporary sqlite database, including two sources sharing a dataset

`tests/test_remember.py` also runs the connector through `cognee.remember` itself, with the LLM and embeddings stubbed (the same technique as the Google Drive connector's forget test). It checks that:

- new pipelines reach the dataset and the graph
- a finished pipeline replaces its earlier document instead of duplicating it
- a dropped project's pipelines leave the graph while another project's stay, even when it was the dataset's last project
- two sources with their own `resource_name` share a dataset without forgetting each other's pipelines
