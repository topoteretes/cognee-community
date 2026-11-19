# CircleCI API fixtures

Real CircleCI API v2 responses, captured on 2026-10-08 from the public fixture project [rokadepiyush49-rgb/cognee-circleci-fixture](https://github.com/rokadepiyush49-rgb/cognee-circleci-fixture) (slug `gh/rokadepiyush49-rgb/cognee-circleci-fixture`).

`index.json` maps each request path (relative to `https://circleci.com/api/v2`) to its file and HTTP status, so a fake session can answer `GET <path>` by lookup.

| Dir | Pipeline | What it covers |
|---|---|---|
| `main/` | #1, failed | `test` fails with 3 tests, `smoke` fails with no test results (`tests_build-and-test_smoke.json` is empty), `deploy` is `not_run` |
| `green/` | #2, success | Every job passes, no tests fetched |
| `many-failures/` | #3, failed | 28 failing tests in one job (per-job cap) |
| `slow/` | #4, success | A `slow-check` workflow that was `running` at sync time and has since finished |
| `hold/` | #5, unfinished | `needs-approval` is `on_hold`: the approval job is `on_hold`, `deploy` is `blocked` |
| `errors/` | | 404 body for a project that doesn't exist |
| `slow-running/` | #6, unfinished | Captured mid-run: `slow-check` and its `slow` job are `running` (`stopped_at` is null), `build-and-test` already failed. Its `pipelines.json` lists #6 first. Has its own `index.json`. |
| `slow-finished/` | #6, success | The same pipeline after `slow-check` finished (`success`). Pair it with `slow-running/` for the re-check test: unfinished on one sync, final on the next. Has its own `index.json`. |

Each pipeline's `state` is `created` even once its workflows are done, so "finished" comes from the workflow statuses.
