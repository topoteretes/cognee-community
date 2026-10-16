# cognee-community-connector-circleci

A CircleCI data-source connector for [cognee](https://github.com/topoteretes/cognee): sync pipeline, workflow and job outcomes into memory, so an agent can answer "what broke on main last night, and why?". It syncs incrementally and forgets projects that are deleted upstream.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each pipeline becomes one **document** that goes through cognee's normal cognify pipeline. Failed jobs carry their failing tests; build logs are never read, because they are large and mostly noise.

## Requirements

- cognee 1.4.0 (pinned in `pyproject.toml`): the first release with document mode (`DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources` routing that reads it).
- Python 3.11 to 3.13.

## Install

```bash
uv pip install cognee-community-connector-circleci
# or, from this monorepo:
cd packages/connector/circleci && uv sync
```

## Usage

```python
import cognee
from cognee_community_connector_circleci import circleci_source

await cognee.remember(
    circleci_source(project_slugs=["gh/acme/api"]),  # CIRCLECI_TOKEN from env, or token=...
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
| `token` | `CIRCLECI_TOKEN` | CircleCI personal API token |
| `branch` | all branches | Only sync pipelines on this branch |
| `max_initial_pipelines` | `50` | Pipelines ingested on a project's first sync, most recent first. `None` ingests the whole history. Later syncs take everything since the last one. |
| `pending_timeout_days` | `7` | Stop re-checking a pipeline that is still unfinished after this long (e.g. an approval nobody clicks) |
| `max_failing_tests` | `20` | Failing tests kept per failed job |
| `max_message_chars` | `500` | Characters kept from the **end** of each failure message, where the error and its location are |
| `base_url` | `https://circleci.com/api/v2` | API base URL. Pipeline links in the documents always point at `app.circleci.com`. |

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
- test: failed
  Failing tests (2):
  - tests/test_client.py::test_retry_after
      def test_retry_after():
      >       assert client.calls == 3
      E       assert 1 == 3
      tests/test_client.py:42: AssertionError
  - ...
- smoke: failed (no test results)
- lint: success
- deploy: not_run
```

Passing jobs are a single status line, and test results are only fetched for failed jobs. Durations and timestamps other than the creation time are left out, so a finished pipeline's text never changes and is never re-cognified.

## How sync + forget-on-delete work

- **Incremental sync.** Each project keeps a `created_at` cursor in dlt's per-resource state. Pipelines are listed newest first, and the listing stops at the cursor, so a run with nothing new costs one listing request per project, plus a re-check of any pipelines that were unfinished last time. Rows are upserted by pipeline id with `write_disposition="merge"`.
- **Unfinished pipelines.** A pipeline still running at sync time is ingested and remembered in state. Later syncs re-check it and ingest it again only once it has finished, so its final status lands without re-ingesting every in-between state. "Finished" is decided from the workflows: a pipeline's own `state` stays `created` while they run.
- **Forget-on-delete.** CircleCI has no way to delete a single pipeline, so deletes happen per project. When a project's pipeline listing returns 404 (the project was deleted, or the token can no longer see it), or its slug is dropped from `project_slugs`, every pipeline ingested for it is emitted with the `_deleted` hard-delete marker. dlt removes those rows on `merge`, and cognee's `orphan_cleanup` purges them from the graph, vector and relational stores.
- **Expired pipelines are kept.** A pipeline that ages out of CircleCI's retention stays in memory as history.
- **Other errors never delete.** An invalid or missing token gets a 401 and fails the run with memory untouched. Rate limits (429) and server errors are retried, honouring `Retry-After`.

Three things to keep in mind:

- A 404 also covers a project the token's user has lost access to. That project is forgotten, just as if it were deleted.
- Sync each dataset with its full list of projects. A project left out of `project_slugs` is forgotten on that run.
- **A dataset is never emptied by a sync.** cognee skips its orphan cleanup when a sync would leave a dataset with no documents, because it can't tell that apart from a broken sync. So if the deleted project was the only one in its dataset, the connector still emits the deletes, but cognee keeps that project's documents. Remove them with `await cognee.forget(dataset="circleci")`. This is cognee's behaviour for every document connector, not specific to CircleCI.

## Setup

1. Create a personal API token under **User Settings → Personal API Tokens** in CircleCI. It needs read access to the projects you sync.
2. Find each project's slug under **Project Settings → Overview**.
3. For failing-test output, the project's `.circleci/config.yml` must save test results with a `store_test_results` step. Without it, a failed job is ingested as `failed (no test results)`.
4. Set `CIRCLECI_TOKEN` (or pass `token=...`), plus your `LLM_API_KEY` like any other cognee run:

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

No live token is needed. The tests replay real CircleCI API responses recorded from a public fixture project (see `tests/fixtures/README.md`), covering failed, passing, on-hold and mid-run pipelines. They cover the HTTP retries, the document text, the cursor, re-checking unfinished pipelines, forget-on-delete, and full runs through a dlt pipeline into a temporary sqlite database.

`tests/test_remember.py` also runs the connector through `cognee.remember` itself, with the LLM and embeddings stubbed (the same technique as the Google Drive connector's forget test). It checks that new pipelines reach the dataset and the graph, that a finished pipeline replaces its earlier document instead of duplicating it, and that a deleted project's pipelines leave the graph while another project's stay.
