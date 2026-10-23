# cognee-community-connector-github-actions

A GitHub Actions data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your CI/CD history into memory — "ask my pipelines".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Workflow
definitions, run attempts, and job results are ingested as **normal documents**
(they flow through cognee's cognify entity-extraction pipeline, not the
deterministic dlt-row path), via cognee's document-mode marker.

## What gets ingested

| Document type | Content |
|---|---|
| **Workflow definition** | Triggers, job names, runner labels, step names, `uses:` action references. Raw shell scripts and env/secrets are excluded. |
| **Run attempt** | Branch, commit SHA, event, actor, attempt number, status, conclusion, duration, URL. Each re-run (attempt 2, 3 …) is a separate document. |
| **Job** | Step names + conclusions, check-run annotations (optional), tail of the failing step's log (optional, ANSI-stripped). |

## Requirements

> **This connector requires a cognee release that ships "document-mode"** —
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the
> `resolve_dlt_sources` routing that reads it. The `cognee==` pin in
> `pyproject.toml` is a placeholder; update it to the first release that
> includes document-mode before publishing.

## Install

```bash
pip install cognee-community-connector-github-actions
# or, from this monorepo:
cd packages/connector/github-actions && uv sync --all-extras
```

## GitHub token setup

Create a **fine-grained personal access token** at
<https://github.com/settings/tokens?type=beta> and grant the following
**repository permissions** (read-only):

| Permission | Required for |
|---|---|
| **Actions** | Workflow runs, jobs, logs |
| **Contents** | Reading `.github/workflows/` |
| **Checks** | Annotations on failed jobs (set `include_annotations=False` to skip) |

Export the token before running:

```bash
export GITHUB_TOKEN="github_pat_..."
export LLM_API_KEY="sk-..."   # your cognee LLM key
```

## Usage

```python
import cognee
from cognee_community_connector_github_actions import github_actions_source

source = github_actions_source(
    repositories=["owner/repo"],   # one or more "owner/repo" strings
    token="github_pat_...",        # or set GITHUB_TOKEN env var
    lookback_days=30,              # how many days of run history to retain
    include_annotations=True,      # requires Checks: Read
    include_log_tail=True,         # tail of failing-step logs
    max_log_lines=100,
)

await cognee.remember(source, dataset_name="github_actions")

answer = await cognee.search(
    query_text="Which workflows failed on main this week and why?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["github_actions"],
)
```

See `examples/example.py` for the full runnable flow.

## How sync + forget-on-delete work

### Workflow definitions
Full snapshot of `.github/workflows/` per sync, with **blob SHA caching**.
Unchanged files are never re-downloaded. Files removed from the directory
receive tombstones and are forgotten by cognee's `orphan_cleanup`.

### Run attempts
Incremental merge over a configurable **lookback window**. Each sync sweeps
the window comparing `(run_id, run_attempt, status, conclusion)` against saved
state. In-progress attempts are skipped; completed ones are stored once and
never re-fetched. Re-runs (higher `run_attempt`) are captured as independent
documents so flaky CI history is fully queryable.

**1,000-result limit**: GitHub caps filtered run searches at 1,000 results.
The connector automatically subdivides the time window recursively until every
run can be retrieved without hitting the cap.

### Jobs
Fetched per completed attempt via GitHub's attempt-specific endpoint (so each
attempt is captured independently, not just the latest). For failed jobs,
**annotations** (from the Check Runs API) and a **log tail** of the first
failing step are included as additional context.

### Lookback expiry + upstream deletion
Runs that cross the lookback boundary or are explicitly deleted on GitHub are
tombstoned. Cognee's `orphan_cleanup` then removes their documents from the
graph and vector stores.

**Safety guarantee**: a partial API response or failed pagination page never
triggers tombstones — the sweep must complete fully before any reconciliation
runs. A run missing because of an API error is never misinterpreted as deleted.

## Rate limits

The connector uses **conditional requests** (ETag / `If-None-Match`) for
workflow file lookups, so unchanged files don't burn rate-limit quota.
`Retry-After` and `X-RateLimit-Reset` headers are honoured, with bounded
exponential backoff for transient failures (5xx, network errors).

## Testing

```bash
cd packages/connector/github-actions
uv run pytest tests/ -v
```

All tests use a fully mocked `FakeGitHubClient` — no live token required. They
cover:
- New/changed/removed workflow files (blob SHA caching)
- New completed runs, in-progress runs skipped, re-runs captured independently
- 1,000-result limit → time-window subdivision
- Job steps, annotations, log tails, expired logs
- Tombstones for removed workflows, deleted runs, orphaned jobs
- ANSI stripping in log tails
- Partial-failure safety (no tombstones on API errors)
- document-source marker routing

## Privacy notes

- Raw shell scripts, `env:` values, and `with:` parameters from workflow YAML
  are **never** ingested — only structural metadata (triggers, job/step names,
  `uses:` references).
- Log tails are captured only for **failing steps** and are **ANSI-stripped**
  before ingestion.
- The temporary log redirect URL is never persisted or logged.
- Set `include_log_tail=False` to disable log ingestion entirely.
- Set `include_annotations=False` if you prefer not to grant Checks: Read.
