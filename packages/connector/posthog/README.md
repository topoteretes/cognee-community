# cognee-community-connector-posthog

A [PostHog](https://posthog.com) data-source connector for
[cognee](https://github.com/topoteretes/cognee). It turns the curated layer of a
product-analytics project into memory, so an agent can answer "What did the checkout
experiment conclude?", "Which flags are rolled out to half of users?" or "How many
people signed up last week, by browser?".

It syncs saved insights (with the query in words and a summary of the latest cached
result), dashboards, cohorts, feature flags and experiments, one document each. Syncs
are incremental, and objects deleted in PostHog are forgotten. The raw event stream
and person profiles are never read.

## Requirements

- Python 3.10–3.14
- `cognee>=1.6.3,<2`, `dlt[sqlalchemy]`, `requests`
- A PostHog project (US Cloud, EU Cloud or self-hosted) and a personal API key

## Install

```bash
# Not published on PyPI yet; install from this monorepo:
cd packages/connector/posthog && uv sync
```

## Setup

1. In PostHog, open **Settings → Personal API keys → Create personal API key**. Give it
   read access to the kinds you sync: `insight:read`, `dashboard:read`,
   `cohort:read`, `feature_flag:read`, `experiment:read`.
2. Note the project id, the number in `https://us.posthog.com/project/<id>`.
3. Pass them to `posthog_source`. The source never reads the environment itself; the
   example below (and `examples/example.py`) reads them from environment variables:

```bash
export POSTHOG_PROJECT_ID=12345
export POSTHOG_API_KEY=phx_...
export POSTHOG_HOST=https://eu.posthog.com   # optional; US Cloud by default
export LLM_API_KEY=sk-...
```

## Usage

```python
import os

import cognee
from cognee_community_connector_posthog import posthog_source

await cognee.remember(
    posthog_source(
        project_id=os.environ["POSTHOG_PROJECT_ID"],
        api_key=os.environ["POSTHOG_API_KEY"],
        host=os.environ.get("POSTHOG_HOST", "https://us.posthog.com"),
        kinds=["insights", "dashboards", "cohorts", "feature_flags", "experiments"],  # optional
    ),
    dataset_name="posthog",
    write_disposition="merge",  # required, see below
    self_improvement=False,
)
```

See `examples/example.py` for a runnable version.

| Argument                    | Meaning                                                                              | Default                          |
| --------------------------- | ------------------------------------------------------------------------------------ | -------------------------------- |
| `project_id`                | The PostHog project number                                                           | required                         |
| `api_key`                   | Personal API key (a pasted trailing newline is stripped)                             | required unless `service` is set |
| `host`                      | US Cloud, EU Cloud or a self-hosted URL                                              | `https://us.posthog.com`         |
| `kinds`                     | A list of any of `insights`, `dashboards`, `cohorts`, `feature_flags`, `experiments` | all five                         |
| `resource_name`             | dlt table name, which also scopes the sync state (keyword-only)                      | `posthog_<project_id>`           |
| `check_active`              | Host checkpoint, called before every request and before every row is published       | none                             |
| `service` (alias `session`) | Pre-built `requests`-compatible session (tests, proxies); not closed by the source   | one per run, closed afterwards   |

Each project gets its own table and sync state, so several projects can share one
dataset without forgetting each other's documents. If two hosts (say, US Cloud and a
self-hosted instance) have a project with the same number, give each its own
`resource_name`.

After a run, `source.cognee_sync_stats` holds counts only: `scanned`, `skipped`
(unchanged), `changed`, `deleted`, `failed` (objects without an id), `failed_auth` and
`failed_rate_limit` (1 when the run stopped for that reason), `requests`, and
`complete` (1 once the run finished).

## What a document looks like

```text
PostHog insight Weekly signups.
New accounts per week.
Query: Trends of signed_up broken down by $browser from -30d.
On dashboards: Growth.
Latest cached result:
signed_up (Chrome): 120.
signed_up (Safari): 45.
```

- **Insights** have their query in words (trends, funnels, retention and so on, with
  events, breakdown and date range, or the SQL of a SQL insight), the dashboards they
  are on, and a summary of the cached result for trends and funnels: values per series,
  or funnel steps with the overall conversion. Only saved insights are synced.
- **Dashboards** list the insights on them.
- **Cohorts** have their size and criteria ("plan exact pro or email is set").
- **Feature flags** have their status, release conditions with rollout percentages,
  variants, and the experiments that use them.
- **Experiments** have their hypothesis, flag, variants, dates, and conclusion.

## How sync and forget-on-delete work

Pass `write_disposition="merge"` to `remember`: this source only emits documents that
changed and deletes by emitting `_deleted` tombstone rows, which only `merge` applies.

- **Incremental sync.** Each run lists the selected kinds through
  `/api/projects/<id>/<kind>/`, following the `next` links, and renders every object.
  A hash of each rendered document is kept in dlt state, so only documents that changed
  are emitted. PostHog has no change feed, and hashing the rendered text also catches a
  refreshed insight result or a renamed dashboard that other documents mention.
- **No recomputation.** Insights are listed with `refresh=force_cache`, which returns
  the result PostHog has cached and never starts a query, so a sync costs no compute.
- **Deletions.** An object marked `deleted`, or missing from the listing, is
  tombstoned and cognee's orphan cleanup forgets it. Removing a kind from `kinds`
  forgets its documents.
- **Failures.** 429 and 5xx responses and dropped connections are retried, up to
  three attempts per request, honouring `Retry-After` (capped at 60 seconds). A run
  waits at most 60 seconds in total, because retries sleep inside cognee's staging lock
  that other ingestions wait on; past that it stops with `failed_rate_limit`. Any failed
  request aborts the run before anything is emitted, so a partial listing never looks
  like a deletion, and the next run starts over. A 401 or 403 names the key's scopes, a
  404 points at the project id, and a `next` link that repeats stops the run. Error
  messages and logs carry the status and path only, never the key or the response body.
- **Text.** Control characters and invalid Unicode are dropped from documents, so a
  stray NUL in a description can't fail the load.
- **Safety net.** If the listing comes back empty for the same kinds as the last run,
  which is what a revoked key or the wrong project looks like, the run stops instead of
  forgetting everything. Changing `kinds` still forgets what is gone.

**Limits:** each run lists every object of the selected kinds (a few requests per kind
for typical projects). Insight results are summarised for trends and funnels; other
insight types are described by their query only. Experiment results come from the
experiment's conclusion, not from a fresh analysis.

## Testing

```bash
uv run pytest tests/
```

The tests use a fake PostHog API, so no account is needed. They cover rendering of
every kind, paging, retries and the wait budget, error hints, change detection
(including a refreshed insight result and a renamed dashboard), deletions and scope
changes, the safety net, stats and `check_active`, that the key never shows up in
errors or logs for 401, 403, 404, 429 and 5xx, and full dlt `merge` runs into a
temporary SQLite destination, including two projects in one dataset.
