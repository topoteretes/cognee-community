# cognee-community-connector-bitbucket

A Bitbucket **Cloud** data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your repositories' pull requests and PR comments into memory — "ask my pull requests".

> **Bitbucket Cloud only.** This connector talks to `api.bitbucket.org`. Bitbucket Server /
> Data Center (the self-hosted product) uses a different API and is not supported.

> **Scope: pull requests and PR comments only — no wiki.** Bitbucket Cloud's native Wiki
> feature has been fully removed by Atlassian; there is no longer any wiki content to
> ingest, on any workspace. This is a hard limitation, not a "coming in a future phase"
> placeholder.

It exposes a `dlt` source you hand to `cognee.remember(...)` / `cognee.add(...)`. Pull
requests and comments are ingested as **normal documents** (they flow through cognee's
cognify entity-extraction pipeline, not the deterministic dlt-row path), via cognee's
document-mode marker.

## Requirements

This connector requires a cognee release that ships "document-mode" — i.e.
`cognee.tasks.ingestion.dlt_utils.DOCUMENT_SOURCE_ATTR` and the `resolve_dlt_sources`
routing that reads it. The `cognee==1.4.0` pin in `pyproject.toml` is the first release
that includes it.

## Install

```bash
uv pip install cognee-community-connector-bitbucket
# or, from this monorepo:
cd packages/connector/bitbucket && uv sync --all-extras
```

## Usage

```python
import cognee
from cognee_community_connector_bitbucket import bitbucket_source

await cognee.remember(
    bitbucket_source(
        workspace="my-team",
        repo_slugs=["my-repo"],  # omit to sync every repo in the workspace
    ),
    dataset_name="bitbucket",
    primary_key="id",
    # "merge" is required: it's what makes re-runs incremental and what makes
    # deletions propagate via orphan cleanup. The add pipeline's default
    # ("replace") would re-fetch and re-cognify everything on every run instead
    # -- this is a cognee.remember()/cognee.add() kwarg, not something the
    # connector's own dlt resource declaration can set on your behalf.
    write_disposition="merge",
    max_rows_per_table=0,
)

answer = await cognee.search(
    query_text="What did we decide in the recent pull requests?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["bitbucket"],
)
```

Scope what you ingest with `repo_slugs=[...]`; omit it to sync every repository the
credential can see in the workspace. Restrict which pull request states are pulled with
`pr_states=(...)` (defaults to all of `OPEN`, `MERGED`, `DECLINED`, `SUPERSEDED`). See
`examples/example.py` for the full flow.

## Auth

**App passwords are deprecated by Atlassian and are not supported by this connector.** Use
one of:

1. **A personal API token with scopes** (recommended) — pass `email=` + `api_token=`
   (sent as HTTP Basic auth), or `api_token=` alone (sent as a bearer token).
2. **Any bearer token** — pass `access_token=` for an OAuth 2.0 access token obtained
   elsewhere, or a Bitbucket repository/workspace access token. This connector does not
   implement an OAuth authorization flow itself; it only accepts a token you already have.

Pass exactly one of `access_token=` or (`email=` + `api_token=` / `api_token=` alone) —
combining `access_token=` with `api_token=` raises an error. Each also falls back to an
environment variable: `BITBUCKET_EMAIL`, `BITBUCKET_API_TOKEN`, `BITBUCKET_ACCESS_TOKEN`.

### Creating a personal API token

1. Click your profile icon in Bitbucket (or any Atlassian product) → **Account settings**.
2. Open the **Security** tab → **Create and manage API tokens**.
3. **Create API token with scopes**, give it a name and an expiry (Atlassian caps this at
   **1 year** — there is no non-expiring option).
4. Select **Bitbucket** as the app, then select these three scopes:
   - `read:workspace:bitbucket`
   - `read:repository:bitbucket`
   - `read:pullrequest:bitbucket`
5. Review and create, then **copy the token immediately** — Bitbucket will not show it to
   you again.

If you're using an OAuth 2.0 access token or an access token instead, the equivalent
scopes are `account`, `repository`, and `pullrequest`.

## How sync + forget-on-delete work

Sync is **incremental, with a periodic full reconciliation pass** — the same shape as the
Google Drive and Confluence connectors. `write_disposition="merge"` upserts rows by their
stable id and physically removes any row marked `_deleted=True` ("hard delete"); both are
controlled by the `write_disposition="merge"` kwarg you pass to `cognee.remember()` /
`cognee.add()` (see Usage above), not by anything the connector sets on your behalf.

- **The common case — incremental.** Pull requests are listed sorted newest-updated-first
  and only those at or after a stored per-repository cursor are re-fetched. Comments are
  re-checked for every pull request touched this run, **plus every pull request still known
  to be `OPEN`** — a comment can be added, edited, or deleted without necessarily bumping its
  parent pull request's own `updated_on` (this is not confirmed either way against a live
  Bitbucket workspace, so the connector makes the conservative, safe assumption and checks
  anyway).
- **Periodically — a full pass.** On the first run, and every `full_sync_every` runs after
  that (default **10**, configurable via `bitbucket_source(..., full_sync_every=N)`;
  `N=1` means every run is a full pass), every pull request and every one of their comments
  is re-listed in full and diffed against what was stored. This is what catches the things an
  incremental pass cannot:
  - A comment deleted on a pull request that has been closed for a while (an incremental pass
    only re-checks `OPEN` pull requests plus ones that changed this run).
  - A pull request that fell out of scope because you narrowed `pr_states`.
  - A repository removed from `repo_slugs`, or no longer visible in the workspace.

  Between full passes, these three cases are simply **not yet forgotten** — they are detected
  and cleaned up at the next full pass, not instantly. Lower `full_sync_every` (down to `1`)
  if you need that window shorter, at the cost of a full listing (and the request volume that
  implies) every run.
- **Pull requests cannot be deleted in Bitbucket** (confirmed against the Cloud REST API —
  there is no delete endpoint for a pull request). A pull request document only ever
  disappears because it fell outside `pr_states` (detected on a full pass), never because it
  was "deleted," since that cannot happen.
- **Comments can be deleted or edited to empty.** A comment the API reports as `deleted`, and
  one that has simply vanished from the listing entirely, are treated identically — both
  result in a tombstone row that `write_disposition="merge"` physically removes, which
  cognee's existing `orphan_cleanup` then forgets from the graph and vector stores, the same
  mechanism the Google Drive connector relies on.
- **Empty-sweep guards.** If every pull request checked for comments in one run comes back
  with zero live comments while some were previously known, that is treated as one probable
  transient failure (a network blip, a revoked credential, …) rather than "every comment was
  deleted at once" — nothing is tombstoned that run, and the previously known comments stay
  in memory. The same logic applies at the pull-request level (an empty full listing is never
  trusted as "every pull request was deleted") and at the repository level (an empty workspace
  listing is never trusted as "every repository was deleted"). The tradeoff: a real, repo-wide
  mass deletion that happens to land exactly when this guard is checking will be delayed by
  one run rather than applied immediately — this is deliberate, since the alternative (acting
  on an ambiguous empty result) risks wiping memory on a transient hiccup instead.
- Unchanged pull requests and comments keep a stable content-hash id, so a no-op resync is
  not re-ingested or re-cognified.
- **A sync is all-or-nothing.** A network failure, an exhausted retry budget, or a 404 on a
  repository slug you explicitly configured aborts the whole run with an error. Each
  repository's new cursor/known-id state is only committed after that repository's listing
  has completed successfully, and the pipeline-level state manager rolls back the entire run's
  state to its pre-run value if any exception reaches it — so a partial listing can never be
  mistaken for "everything else was deleted."

Rate limits: Bitbucket Cloud's baseline authenticated allowance is **1,000 requests/hour** per
token (more on larger paid plans). This connector retries 429 and 502/503/504 responses with
backoff before giving up.

## Setup

1. Create (or reuse) a Bitbucket Cloud workspace and note its workspace id (the slug in
   `bitbucket.org/<workspace>/...` URLs).
2. Create a personal API token with the scopes above (or obtain a bearer token another
   way).
3. Export the credential and your LLM key, then run the example:

   ```bash
   export BITBUCKET_WORKSPACE="my-team"
   export BITBUCKET_EMAIL="you@example.com"
   export BITBUCKET_API_TOKEN="..."
   export LLM_API_KEY="sk-..."
   uv run python examples/example.py
   ```

## Testing

```bash
uv run pytest tests/
```

The tests mock the Bitbucket API (no live credential) and cover credential resolution,
pagination, retry/error handling, pull-request state filtering, comment filtering
(deleted / empty / zero-comment-count), deterministic rendering, the incremental cursor
and full-pass reconciliation logic (including the empty-sweep guards and mid-run-failure
safety), and forget-on-delete through a real `dlt` pipeline run — including, in
`tests/test_bitbucket_forget.py`, a real `cognee.cognify()` pass proving a deleted
comment's extracted entity is actually removed from the graph, not just the staging table.
