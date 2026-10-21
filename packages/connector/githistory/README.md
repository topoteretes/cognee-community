# cognee-community-connector-githistory

A local Git repository history connector for [cognee](https://github.com/topoteretes/cognee):
sync a repo's commit history into memory — "ask my codebase's history why".

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Commits are
rendered to a short document (subject, body, author, date, changed files, trailers) and
ingested as **normal documents** (they flow through cognee's cognify entity-extraction
pipeline, not the deterministic dlt-row path), via cognee's document-mode marker — same
pattern as the Notion connector.

Unlike the hosted-forge connectors (GitHub, GitLab, Bitbucket), this reads a **local clone**
directly with the `git` binary: no API token, no rate limit, no network call, and it works
with any host, including a repo that was never pushed anywhere.

## Requirements

> **This connector requires a cognee release that ships "document-mode"** — i.e.
> `cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
> routing that reads it. The `cognee==` pin in `pyproject.toml` is a placeholder; set it to
> the first release that includes document-mode before publishing.

## Install

```bash
uv pip install cognee-community-connector-githistory
# or, from this monorepo:
cd packages/connector/githistory && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_githistory import githistory_source

await cognee.remember(
    githistory_source("/path/to/local/repo"),
    dataset_name="githistory",
)

answer = await cognee.search(
    query_text="Why do we retry three times in the payment client?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["githistory"],
)
```

`githistory_source(...)` options:

- `repo_path` — path to a local clone (required). A shallow clone is rejected.
- `refs` — revisions to walk, e.g. `["main"]`. Defaults to `["HEAD"]`.
- `paths` — restrict history to commits touching these paths.
- `include_diffs` — include per-commit patch text, capped at `diff_byte_cap` bytes. Off by
  default; most of the value is in the commit message, and diffs are large.
- `include_bots` — include commits from common bot accounts (dependabot, renovate,
  github-actions). Off by default.

See `examples/example.py` for the full flow.

## How sync + forget-on-delete work

A commit SHA is permanent once created — it is never edited, only abandoned. So this is an
**incremental, SHA-keyed merge sync**, not a full-snapshot like Notion/Slack:

- `git rev-list <refs>` lists every commit SHA currently reachable from the configured refs.
  This is cheap (no commit bodies), so it runs in full on every sync.
- SHAs already seen (tracked in dlt resource state) are skipped; only new commits are
  rendered and ingested (`write_disposition="merge"`).
- SHAs that were ingested before but are no longer reachable — a rebase, a force-push, a
  `git filter-repo` pass to scrub a leaked secret, a deleted branch — are emitted as
  `_deleted` hard-delete tombstones, so cognee removes them from the graph and vector stores
  on the next sync. This matters most for the secret-removal case: the commit has to actually
  leave memory, not just stop appearing in new syncs.

**Safety rule:** the full reachable-SHA listing must succeed before any tombstone is
emitted. A git failure partway through a run (corrupt repo, missing object) aborts with
nothing deleted — a partial reachable-set is never treated as "everything else was removed".
A **shallow clone is refused outright**, for the same reason: its history is truncated by
design, so on the very first run every commit before the shallow boundary would look deleted.
Run `git fetch --unshallow` first.

## What gets ingested

One document per commit: subject, full body, author, date, the changed-file list with
add/remove counts, any Git trailers in `Key: value` form (`Fixes: #123`, `Co-authored-by: ...`), and — for merge
commits — the parent SHAs and ref names pointing at it. Diff bodies are off by default and
size-capped when enabled, so one huge commit (a dependency bump, a generated-file change)
can't dwarf every other document in the dataset.

Bot commits (dependabot, renovate, github-actions) are skipped by default — they add little
to a "why did we do this" memory — but can be included with `include_bots=True`.

## Limitations / what I've verified

- Tested against repos built in a temp directory covering: a merge commit, a trailer block,
  a rebase that drops a commit (checked it tombstones correctly), a shallow clone (checked
  it is refused), a root commit with no parent, a non-UTF-8 commit message, and a diff over
  the size cap.
- **Measured against [pallets/flask](https://github.com/pallets/flask)** (5,562 commits,
  cloned full history). `git rev-list` listing every reachable SHA: 0.06s. First full sync:
  151s (≈27ms/commit — two `git` subprocess calls per commit, for the commit metadata and
  the file stat). A no-op resync with nothing new: 0.17s, confirming the incremental skip
  actually avoids re-rendering unchanged commits. 191 commits were correctly skipped as bot
  commits (`dependabot[bot]`, `dependabot-preview[bot]`, `pre-commit-ci[bot]`), matching the
  real count in that repo's history. I have **not** tested a repository with hundreds of
  thousands of commits; at the measured per-commit rate that would take tens of minutes on
  first sync, and the straightforward fix if that's too slow is batching multiple commits
  per `git show` call instead of one process each.
- Squash-merge-only workflows put most of the real discussion in one commit body, which this
  handles fine; a workflow that keeps long-lived merge commits with their own messages is
  also preserved as a separate document rather than being flattened away.

## Setup

No account or token needed — point `repo_path` at any local Git clone you can read.
