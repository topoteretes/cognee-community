# cognee-community-connector-github

A GitHub data-source connector for [cognee](https://github.com/topoteretes/cognee): sync your
repositories' issues, pull requests and releases into memory and ask questions about them.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Repositories, issues, pull
requests, releases and (opt-in) commit messages are ingested as **normal documents**, so they
go through cognee's cognify entity extraction. Re-running syncs only what changed, unchanged
listings cost no rate-limit quota, and anything deleted on GitHub is forgotten on the next
sync.

Source files are left alone: to put a repository's code into cognee, hand its URL to
`cognee.add` instead.

## Install

```bash
uv pip install cognee-community-connector-github
# or, from this monorepo:
cd packages/connector/github && uv sync --all-extras
```

The package needs `cognee>=1.6.2,<2`: document-mode routing, the table-scoped orphan
cleanup, `PIPELINE_SCOPE_ATTR` and `guarded_rows` all ship in cognee 1.6.2. The tests pass on
1.6.2 and 1.6.4.

## Setup

1. Create a token at https://github.com/settings/tokens, either:
   - **classic**: scope `repo` for private repositories, or no scope for public ones; or
   - **fine-grained**, limited to the repositories you ingest, with read-only access to
     **Metadata**, **Issues**, **Pull requests** and **Contents** (Contents is for the README
     and commits). These are the permissions GitHub's documentation lists for the endpoints
     used; a token with exactly these was not tested.
2. Export it as `GITHUB_TOKEN` (or pass `token=...`), plus your `LLM_API_KEY` like any other
   cognee run.

The connector is read-only. It only issues `GET` requests. The token is checked before the
first request: one with whitespace or control characters in it (a pasted newline, say) is
refused with a `ValueError` that does not repeat it.

Ingested text goes to the LLM configured for cognee: cognify sends each document to it for
entity extraction.

## Usage

```python
import cognee
from cognee_community_connector_github import github_source

await cognee.remember(
    github_source(repos=["owner/name"]),  # GITHUB_TOKEN from env
    dataset_name="github",
    primary_key="id",
    write_disposition="merge",  # incremental upsert by row id
    max_rows_per_table=0,  # unlimited: orphan cleanup sees the whole corpus
)

answer = await cognee.search(
    query_text="Which open bugs are waiting on a review?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["github"],
)
```

Re-running `remember(...)` with the same dataset syncs only what changed since the last run
and forgets what was deleted. See `examples/example.py` for the full flow.

> **`write_disposition="merge"` is required.** The add pipeline defaults to `"replace"`,
> which would drop every document that is not re-emitted on the second sync.

### Selecting what to ingest

| Argument | Meaning |
|---|---|
| `repos=["owner/name", ...]` | The repositories to ingest. Required. Case does not matter. |
| `include_commits=False` | Also ingest the commit messages of each default branch. Off by default: a busy repository has thousands. |
| `include_bots=False` | Keep comments and reviews written by bots (Copilot's reviewer, github-actions, ...). Off by default: they are long and repetitive. Issues and pull requests opened by a bot are ingested either way. |
| `include_readme=True` | Put the README text into the repository document. |
| `token="..."` | Token. Defaults to the `GITHUB_TOKEN` environment variable. |
| `resource_name="github_documents"` | The dlt table name, which also scopes the sync state. Lowercase snake_case, at most 64 characters. See below. |
| `check_active=None` | Optional callable for a host: asked before every GitHub request and every wait, and around every row; raising stops the sync with nothing deleted. |
| `service=None` | A pre-built `requests`-like session (mainly for tests); `session=` is accepted too. Without it, each run builds its own session and closes it. |

A repository that does not exist, or that the token cannot read, stops the run with an error
and deletes nothing. To forget a repository, remove it from `repos` and sync again.

**Several sources in one dataset.** Each `resource_name` has its own table and its own sync
state, and a sync only ever forgets documents of its own table. Two `github_source(...)` calls
into one dataset (two tokens, or two selections of repositories) therefore need two names,
for example `resource_name="github_work"` and `resource_name="github_oss"`. With the same
name, each run would forget the other's documents.

### What a document looks like

One document per repository, issue, pull request and release (and commit, if enabled):

```text
# Fix the checkout button

Type: pull request
State: merged
Author: ada
Labels: bug
Milestone: v2.0
Branch: fix-checkout -> main

Closes #1.

Comments:
- grace: Reproduced on iOS before the fix.

Reviews:
- lin (approved)
- grace (changes requested): Add a test.

Review comments:
- grace on src/cart.ts: Rename this.

Changed files:
- src/cart.test.ts (+40/-0)
- src/cart.ts (+12/-3)
```

No diffs: a pull request lists its changed files with line counts, at most 100 of them,
followed by `... and N more files`. Labels, assignees and topics are sorted by name, comments
and reviews oldest first and files by path, so the same item always renders the same text. The repository document holds the description, topics,
homepage, language, default branch, the archived flag and the README. A release document
holds its tag, pre-release and draft flags and its notes. Timestamps and counters are kept out
of the text on purpose: an unchanged issue keeps its content hash and is not re-cognified.

## How incremental sync works

Per repository, dlt resource state keeps a cursor, the ETags of every listing page, and a
short fingerprint per issue.

- **The `since` cursor.** `GET /repos/{owner}/{repo}/issues?since=` returns issues and pull
  requests together. Each run asks for everything updated since 5 minutes before the newest
  `updated_at` seen, and renders only those: comments, and for a pull request also reviews,
  review comments and changed files. Adding, editing or deleting a comment, a review or a
  review comment, and pushing a commit, all move the `updated_at` (verified live).
- **ETags.** Every listing page, the repository and the README are requested with the ETag
  stored from the last run. An unchanged one answers `304`, which costs no rate-limit quota. A
  run where nothing changed costs no quota at all, apart from the first incremental run after
  the first sync, which asks for the change listing for the first time.
- **Late timestamps.** GitHub applies `updated_at` a few seconds after the change. That is
  why the cursor overlap is 5 minutes, and why an issue whose document was read within 60 seconds of its
  `updated_at` (by GitHub's own `Date` header on the listing and on the comments, whichever is
  earlier) is fetched once more on the next run instead of
  being marked done.
- **The header fingerprint.** An issue document shows its labels, milestone and assignees by
  name. Renaming a label or a milestone does not move the `updated_at` of the issues that show
  it (verified live). The connector hashes the header lines each issue would show, from the
  listing it already has, and renders an issue again when that hash changes.

Pull requests and issues share one numbering per repository, and one listing.

### What a rename costs

Renaming a label or milestone renders every issue that shows it on the next run: one request
for its comments, plus four more for a pull request (the pull request, its reviews, review
comments and files). GitHub allows 5,000 requests per hour for a token.

## How forget-on-delete works

Every run lists every issue number of each repository (the sweep, oldest first, `state=all`).
A number that was a document before and is missing now is asked for once, without following
redirects:

- `410` means the issue was deleted, and a `301` whose `Location` is an issue in another
  repository means it was transferred. Either way its document is forgotten. (If the other
  repository is also selected, the issue appears there as a new document.) A renamed
  repository also answers `301`, pointing back at itself; that does not count.
- **Anything else keeps the document**, including `404` (which GitHub also answers when the
  token has lost access), `200`, and an error that outlasts the retries. A warning names the
  endpoint and the status, the rest of the run goes on, and the next run asks again. One odd
  issue can neither delete anything nor stall every sync. A number that keeps giving such a
  clear but inconclusive answer (`404`, `403`, `200`, a `301` that is not a transfer) in three
  runs in a row is forgotten; an error that outlasts the retries never counts toward that.

Releases that vanish from the release list are forgotten too, and so is everything of a
repository removed from `repos`. Forgotten ids are emitted with the `_deleted` hard-delete
marker, dlt removes those rows on `merge`, and cognee's `orphan_cleanup` removes them from the
graph and vector stores.

All listings are completed before the first row is emitted, and state is saved only after the
last one. If a listing fails (after retries), or `check_active` raises, the run aborts: nothing
is deleted and no cursor or ETag moves, so the next run starts from the same point. An answer
of `200` with something other than the expected JSON (an HTML login page, say) also aborts the
run, whether it came from a listing or from one issue; it is never read as an empty listing or
an issue to skip.

## When GitHub fails part of the way through

An issue or pull request that is still listed but fails on its own while it is rendered keeps
its previous document and is rendered again on the next run; the rest of the run goes on:

- **`403` or `404`** on its comments, reviews or files: kept. If it answers that way in three
  runs in a row at the same `updated_at`, its document is forgotten and it is not asked about
  again until its `updated_at` moves. A `403` counts toward that only if another item of the
  same kind (issue, or pull request) was read in the same run. When every item of a kind
  answers `403`, the token most likely lacks a permission (a fine-grained token without
  "Pull requests: read" lists pull requests but cannot read them): the items are kept,
  nothing is given up, the run counts them under `failed_permission`, and one warning per kind
  says to check the token's permissions. A rate-limit `403` is not a permission `403`: it
  stops the run cleanly (see Rate limits).
- **A server error or no answer** that outlasts the retries: kept. Three of these in a row on
  fresh issues means GitHub itself is down, and the run stops cleanly (below).

Issues kept from an earlier run are rendered after the fresh ones and never count toward that
streak, so issues that keep failing cannot hold up the others.

Some failures stop the run **cleanly** instead of aborting it: a `401` after at least one
issue was emitted (a token that expires during a long first sync), a rate limit that outlasts
the retries or asks for a wait over 60 seconds (such as the hourly reset of the primary limit),
and the outage above. The run then ends normally: what it emitted is loaded and nothing is
deleted. A repository finished before the stop keeps its new state; for the others the cursor
and ETags stay where they were, so the next run lists the same changes again (unchanged pages
answer `304` and cost no quota) and renders only what this run did not reach. A `401` before
any issue was emitted still raises, since that is a bad token.

Error messages name the status and the endpoint only (`GitHub /repos/acme/site: HTTP 403`).
GitHub's own message is on the exception's `detail`; a response body or the token never is.

### Sync stats

`resource.cognee_sync_stats` holds the counts of the last run, as plain ints: `scanned`
(issues and pull requests listed), `skipped` (unchanged), `failed` (item failures), `deleted`,
`kept` (issues waiting for a retry, and missing numbers not yet shown to be gone) and
`given_up`, plus `failed_auth`, `failed_rate_limit` or `failed_unavailable` when the run stopped
early for that reason, and `failed_permission` (how many items of a kind that could not be read
at all answered `403`).

## Rate limits

`429`, or `403` with `retry-after` or `x-ratelimit-remaining: 0`, is a rate limit: the
connector waits `retry-after` seconds, or until `x-ratelimit-reset` measured against GitHub's
`Date` header, and tries again. A single wait is at most 60 seconds, and `check_active` is
asked before it; a longer one (the primary limit resets hourly) stops the run cleanly, and the
next run carries on. A `403` whose message says "secondary rate limit" but has
neither header is waited out for a minute, as GitHub advises. A `403` without those headers is a permission error and fails
at once (verified live: it carries a non-zero remaining count and no `retry-after`). Server
errors and network failures back off and retry, up to five attempts.

## Limits

- Anyone who can read the cognee dataset can read what was ingested, including issues and
  pull requests of private repositories. Give a dataset only to people who may read all its
  repositories.
- Two tokens or two repository selections in one dataset need different `resource_name`s
  (see above).
- Commit messages are append-only: commits removed from the default branch by a force-push
  are not forgotten, and the shas of all ingested commits are kept in state. Commits brought
  in by a merge commit whose own dates are older than the cursor are missed; the merge commit
  itself is ingested.
- A renamed repository keeps syncing under its old name (GitHub redirects it) and a warning
  asks you to update `repos=`.
- A very long thread makes a very large document, and its LLM cost grows with it. Only the
  pull request file list is cut (at 100 files, and the document says how many more there are);
  comments, reviews and releases are read in full.
- Comments on commits, discussions, projects, wikis and source files are not ingested. An issue
  converted to a discussion stays as a closed issue.
- A comment by a deleted account is attributed to `ghost`.
- An issue that cannot be read in three runs in a row is forgotten until it changes.
- Deletions propagate on a foreground `remember(...)`. cognee skips orphan cleanup for
  `run_in_background=True`.

## Verified against real repositories vs. taken from the docs

Checked against a private throwaway repository and against public `topoteretes` repositories,
2026-10-11 and 2026-10-15:

- `GET /issues?since=` returns issues and pull requests together (pull requests carry
  `pull_request`); `GET /pulls` ignores `since`.
- Timestamps have whole-second resolution and are applied with a delay of several seconds.
- Adding, editing and deleting an issue comment, submitting a review, adding a review comment
  or a reply, and pushing a commit to the branch each move the `updated_at` of the issue or
  pull request.
- Renaming a label (and changing its colour) or a milestone does not move it.
- A page requested with its own ETag answers `304` and `x-ratelimit-used` does not move; once
  any issue on that page changes, the same ETag gets a `200`. The repository object and the
  README answer `304` the same way.
- Pages chain through `Link: rel="next"` URLs with opaque cursors (`after=...`), followed
  exactly as given.
- A deleted issue disappears from the listing at once and answers `410`. A transferred issue
  disappears too and answers `301` with a `Location` in the target repository.
- An issue converted to a discussion (web UI only; there is no API for it) stays in the listing
  as a closed, locked issue and answers `200`, also five minutes later. It stays a closed-issue
  document; the discussion itself is not ingested.
- Copilot's reviewer (`copilot-pull-request-reviewer[bot]`) and `github-actions[bot]` both
  have `user.type == "Bot"`.
- A permission `403` carries a non-zero `x-ratelimit-remaining` and no `retry-after`.
- The connector itself, through a dlt pipeline: first sync, quiet syncs (every request
  answered `304`), an edited comment, a renamed milestone, a submitted review, a README edit
  pushed to the default branch (new commit and repository document), and a deleted issue.

Taken from GitHub's documentation, not reproduced live:

- Rate-limit responses (`429`, or `403` with `retry-after` or `x-ratelimit-remaining: 0`)
  and their headers. The limits were never hit; this path is covered by the mocked tests.
- The fine-grained token permissions listed under Setup.

## Testing

```bash
uv run pytest tests/ -q
```

The tests mock the GitHub API (no token, no network). They cover rendering (also from
shuffled input), the header fingerprint, Link pagination, ETags and `304`s, the cursor and its
overlap, late timestamps, label and milestone renames, bot filtering, the pull request file
cap, the README, commits, deleted and transferred issues, missing issues that are kept and
given up, issues that fail to render (kept and retried, three unreadable ones not blocking a
run and given up after three runs, retries going last), the clean stops (`401` mid-run, rate
limit, an hour-long reset, outage) and what the next run picks up, rate limits versus
permission errors, bodies of the wrong shape, the token never reaching a message or a log
line, and the failure paths that must delete nothing. A real `dlt` pipeline into a temp sqlite
destination checks edits and deletes across runs, two sources in one dataset staying apart, a
`401` mid-run resuming on the next run, and `check_active` stopping a run with nothing loaded.
