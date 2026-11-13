# cognee-community-connector-github-actions

A GitHub Actions data-source connector for [cognee](https://github.com/topoteretes/cognee):
turn your CI history into memory — "why did the deploy workflow keep failing last week?"

It fetches completed workflow runs (and their jobs, and optionally the errors from failed jobs' logs)
for the repositories you choose, renders each run as a markdown document, and exposes a `dlt`
source you hand to `cognee.remember(...)`. It reuses cognee's existing DLT ingestion path
(`resolve_dlt_sources` → `ingest_dlt_source` → `orphan_cleanup`), so you get **snapshot sync**
and **forget-on-delete** with no core changes. Runs are ingested as normal documents (source tag
`github_actions`), so they go through the full cognify entity-extraction pipeline.

## Install

```bash
uv pip install cognee-community-connector-github-actions
# or, from this monorepo:
cd packages/connector/github-actions && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_github_actions import github_actions_source

await cognee.remember(
    github_actions_source(
        repos=["owner/repo"],  # token from GITHUB_TOKEN, or pass token=
        max_runs_per_repo=100,  # snapshot window (default 100)
        include_failed_logs=True,  # opt-in: errors + output of each failed step
    ),
    dataset_name="github-actions",  # use a DEDICATED dataset (see below)
)

answer = await cognee.search(
    query_text="Which workflows failed recently, and why?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["github-actions"],
)
```

See `examples/example.py` for a runnable demo.

### Options

| Argument | Default | Meaning |
|---|---|---|
| `repos` | required | Repositories as `"owner/repo"`. |
| `token` | `GITHUB_TOKEN` | GitHub token (see below). |
| `workflow` | all | One workflow, by file name (`"ci.yml"`) or id. |
| `branch` | all | Only runs on this branch. |
| `created_after` | none | Only runs created on/after `YYYY-MM-DD`. |
| `max_runs_per_repo` | `100` | Most recent N completed runs per repo; `None` for all in range. |
| `include_failed_logs` | `False` | Fetch each failed job's log and keep its errors and failed-step output. |
| `log_tail_lines` | `50` | Output lines kept from each failed step. |
| `max_workers` | `4` | Runs fetched concurrently; `1` fetches serially. |

### Token

A fine-grained personal access token with **Actions: read** on the target repositories, or a
classic token (`repo` scope for private repositories; none needed for public ones beyond rate
limits). Inside a GitHub Actions workflow, the built-in `GITHUB_TOKEN` with `actions: read`
works too.

## What a run looks like in memory

Each run becomes one document titled `"<workflow> #<run number> — <conclusion> (<repo>)"`, with
the event, branch, commit (short SHA + first line of the message), who triggered it, timings,
each job's conclusion, and the names of failed steps. With `include_failed_logs=True`, each failed
job also gets a `Failure log` section:

```text
## Failure log: test
Errors:
- AssertionError: widget did not spin
- Process completed with exit code 1.
Output before the error:
```

followed by the last `log_tail_lines` lines the failed step printed. Runner setup, earlier steps,
the step's command/env header, timestamps, and terminal colour codes are stripped, so memory gets
the failure rather than log noise.

## Performance

Fetching costs one request per 100 runs to list them, one per run for its jobs, and one per failed
job for its log when logs are enabled; 100 runs is roughly 100–130 requests. Runs are fetched
`max_workers` (default 4) at a time. GitHub asks clients to keep concurrency low, and the
secondary rate limits it may hit are retried. In practice, cognify's LLM calls take far longer
than fetching.

## How sync + forget-on-delete work

The source is a full snapshot (`write_disposition="replace"`, cognee's default): each sync
reloads exactly the runs inside the window. A run that falls out of the window, ages out of
GitHub's retention period, or is deleted drops out of the snapshot, and cognee's `orphan_cleanup`
purges it from the graph + vector + relational stores. Run ids (`owner/repo:<run id>`) are
stable, and only **completed** runs are ingested, so an unchanged run keeps its content hash and
is not re-cognified; a re-run changes its attempt and conclusion and is re-synced. Leave
`write_disposition` at the default — passing `merge` breaks forget-on-delete for a snapshot
source.

> **Use a dedicated `dataset_name`.** `orphan_cleanup` scopes deletions per dlt source within a
> dataset; a dedicated dataset guarantees cleanup only ever touches these runs.

API failures never produce a partial snapshot: rate limits (429, rate-limited 403) and server
errors are retried with backoff (honoring `Retry-After` / `X-RateLimit-Reset`, up to 60s), and a
persistent error aborts the sync, leaving staging — and memory — untouched. An expired job log
is skipped, since it only thins a run's document rather than removing the run.

## Testing

```bash
uv run pytest tests/
```

Tests fake the GitHub REST API with `httpx.MockTransport` (no token needed) and include offline
end-to-end runs through a real `dlt` replace load, proving that a run leaving the window is
physically removed and that a failed sync leaves the previous snapshot intact.
