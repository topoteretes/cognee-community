# cognee-community-connector-gitlab

Sync GitLab issues, merge requests, their comments, and wiki pages into
[Cognee](https://github.com/topoteretes/cognee). The connector exposes a
document-mode dlt source for `cognee.remember()`, works with gitlab.com or a
self-managed instance, picks up only changed items on later runs, and removes
documents whose source was deleted upstream. Implements
[cognee issue #4709](https://github.com/topoteretes/cognee/issues/4709).

## Install

Requires Python 3.11–3.13 (Cognee 1.6.5 does not support 3.14) and GitLab 15.5 or
later. From this repository's root:

```bash
cd packages/connector/gitlab
uv sync
```

This installs the package, Cognee, dlt, `httpx`, and the test/lint tools into
the package's `.venv`.

## GitLab setup

1. Sign in to GitLab with an account that can read the projects you want to sync.
2. Open your avatar → **Edit profile** → **Access tokens** (personal access
   tokens), then add a new **legacy** token, not a fine-grained one.
3. Give it a name and an expiration date, select the **`read_api`** scope, and
   create it. Copy the token; GitLab shows it only once.
4. Export it, plus the instance URL if you are not on gitlab.com:

```bash
export GITLAB_TOKEN="glpat-..."
export GITLAB_BASE_URL="https://gitlab.example.com"   # optional; defaults to gitlab.com
```

`read_api` is enough; the broader `api` scope is accepted but not needed. The
connector only reads. Keep the token out of Git and logs. See GitLab's
[personal access token guide](https://docs.gitlab.com/user/profile/personal_access_tokens/).

### Why a legacy token

The connector supports and recommends legacy personal access tokens. One
`read_api` scope gives read access to everything the token's user can see:
every project, with its issues, merge requests, comments, and wiki.

Fine-grained tokens are not supported or tested. Besides choosing which groups
and projects the token reaches, it needs a separate read permission for each kind
of resource the connector calls: Personal Access Token (token check), Project
(discovery), Work Item (issues, and the comments on both issues and merge
requests), Merge Request, and Wiki. Any permission left out makes that part of the
sync fail or come back incomplete. The connector's token check also expects the
`read_api` scope in the token's `scopes` field, while GitLab reports fine-grained
permissions in a separate `granular_scopes` field, so a fine-grained token is
likely rejected before the sync starts. Supporting them would mean testing each
permission combination; that is left out for now.

GitLab can also require fine-grained tokens from a set date. On gitlab.com, a
top-level group that does this blocks legacy tokens from its groups and projects,
so this connector cannot sync them. On a self-managed instance, users can no longer
create or rotate legacy tokens, and existing ones work only until they expire. See
GitLab's
[fine-grained token guide](https://docs.gitlab.com/auth/tokens/fine_grained_access_tokens/).

## Run the example

From `packages/connector/gitlab`, with `GITLAB_TOKEN` and your Cognee model
credentials (for example `LLM_API_KEY`) set:

```bash
uv run python examples/example.py
uv run python examples/example.py --projects my-team/backend,87746024 \
  --resources issues,merge_requests,comments --question "What is blocking the release?"
```

The example syncs into the `gitlab` dataset, waits for processing, and runs a
search. Run it again after editing or deleting an issue, MR, comment, or wiki page
to see the change applied. `--help` lists all options. Without `--projects` (or
`GITLAB_PROJECT_IDS`), it syncs every project the token owner is a member of.

## Python usage

```python
import asyncio
import os

import cognee
from cognee_community_connector_gitlab import gitlab_source


async def main():
    source = gitlab_source(
        token=os.environ["GITLAB_TOKEN"],
        base_url="https://gitlab.com",
        resource_types=["issues", "merge_requests", "comments", "wiki"],
        project_ids=["my-team/backend"],
    )
    result = await cognee.remember(
        source,
        dataset_name="gitlab",
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
        run_in_background=False,
    )
    if result.status != "completed":
        raise RuntimeError(result.error or result.status)


asyncio.run(main())
```

Always pass `write_disposition="merge"` (Cognee defaults to `replace`, which would
erase unchanged rows) and `max_rows_per_table=0` (a row limit would truncate the
inventory used for deletion checks). Use foreground ingestion
(`run_in_background=False`) so Cognee's orphan cleanup runs after deletions, and
use a dedicated dataset. Create a new source for each sync.

### Signature

```python
def gitlab_source(
    token: str,
    *,
    resource_types: Sequence[Literal["issues", "merge_requests", "comments", "wiki"]],
    base_url: str = "https://gitlab.com",
    project_ids: Sequence[int | str] | None = None,
    include_internal: bool = False,
) -> DltSource
```

Everything after `token` is keyword-only. The return value is a document-mode dlt
source to pass straight to `cognee.remember()`; the GitLab content is fetched when
Cognee runs it, not when the source is created.

| Argument | Default | Meaning |
| --- | --- | --- |
| `token` | Required | Legacy personal access token with `read_api` (fine-grained tokens are not supported). |
| `resource_types` | Required | Non-empty selection of `issues`, `merge_requests`, `comments`, `wiki`. `comments` requires `issues` or `merge_requests`. |
| `base_url` | `https://gitlab.com` | Instance URL, without credentials or query string. |
| `project_ids` | `None` | Numeric IDs or `group/project` paths. `None` syncs every project the token owner is a member of. |
| `include_internal` | `False` | Also ingest confidential issues and internal comments. See [Confidential issues and internal comments](#confidential-issues-and-internal-comments). |

`gitlab_source()` validates the legacy token immediately (active, with `read_api` or
`api`) using `GET /personal_access_tokens/self`, so a bad token fails before any
sync starts. That endpoint first shipped in GitLab 15.5, so older self-managed
releases are not supported. It checks the token again when the sync runs and stops
if the token now belongs to a different user.

It raises:

- `ValueError` for invalid arguments: an empty token, an unknown or empty
  `resource_types`, `comments` without `issues` or `merge_requests`, a `base_url`
  with credentials, a query string or a non-HTTP(S) scheme, or an empty or invalid
  `project_ids` entry, or an `include_internal` that is not a bool.
- `GitLabAPIError` (a `RuntimeError`, importable from
  `cognee_community_connector_gitlab.gitlab`) when GitLab rejects the token, the
  token is inactive or lacks `read_api`, or a request fails (network error,
  timeout, 403, 429, 5xx, or invalid JSON).
- `RuntimeError` when the installed Cognee lacks document sync support.

## What gets ingested

One document per issue, merge request, or wiki page, with `id`, `title`,
`content`, and `url`. Metadata is rendered into `content`, which is the text
Cognee processes:

| Resource | Content |
| --- | --- |
| Issue | Project, reference (`#iid`), state, author, labels, source URL, description |
| Merge request | The same, with `!iid` and source/target branches |
| Wiki page | Project, format, source URL, and the page's original markup |

MR commits, diffs, attachments, and repository files are not ingested. Only
project-level content is read: group wikis, epics, snippets, releases, and CI/CD
data are out of scope. Wiki markup is kept as-is, not converted to Markdown.

### Comments

Issue and MR payloads only carry a comment count, so with `comments` selected
the connector reads `/projects/:id/issues/:iid/notes` (or `merge_requests`) and
appends a `## Comments` section to the parent document, oldest first:

```
alice (2026-10-12):
I think the bug is in the auth middleware.

bob (2026-10-13):
Confirmed, fixed in !28.
```

System notes ("added label", "mentioned in") are skipped. Every other user
comment is kept, except internal comments unless `include_internal=True`; Cognee
chunks long documents like any other. Deleting an issue or MR removes
its comments too.

### Confidential issues and internal comments

By default (`include_internal=False`) the connector leaves out confidential
issues and internal comments, because anyone who can query the destination
dataset can read what is in it. An issue that becomes confidential after it was
synced is removed on the next run, like a deleted issue.

With `include_internal=True`, both are ingested and each internal comment is
marked `[internal]` in the text (`bob (2026-10-13) [internal]:`). Restrict access
to the dataset accordingly. The setting is part of the staging scope, so switching
it builds a separate set of documents instead of leaving earlier ones rendered
under the old setting; use a separate dataset for each value.

## Choosing projects

Without `project_ids`, discovery uses keyset pagination on
`GET /projects?membership=true`: the token owner's own projects and projects
shared with them directly or through a group, never unrelated public projects.

With `project_ids`, each entry is read with `GET /projects/:id` (paths are
URL-encoded, so `team/app` becomes `team%2Fapp`). A numeric ID and a path naming
the same project are synced once. A selected project that returns 404 is logged
and skipped, and documents synced from it earlier are removed. Each distinct
selection has its own staging scope, so changing the selection starts a separate
corpus rather than deleting documents from the previous one.

A project can turn off its issues, merge requests, or wiki. The connector skips
only that resource for that project, detected from project metadata
(`<feature>_access_level: "disabled"`, or `<feature>_enabled: false` on older
releases) or a 403/404 on the first list page. Documents synced earlier from that
resource are removed. A 403/404 on a later page still aborts the run, because the
inventory would be incomplete.

## Sync behavior

- **First run:** reads every selected resource and stores a baseline in dlt
  resource state.
- **Issues and MRs:** later runs request items with `updated_after` set to the
  greatest `updated_at` seen, minus a one-minute overlap, per project and
  resource. Because GitLab has no deletion feed, each run also reads the full
  unfiltered issue/MR list to find deleted items. Whether a document is emitted
  is decided by a hash of its rendered content, so the `updated_after` request
  adds one request per resource and does not reduce traffic. It is kept because
  the issue specifies it as the incremental cursor. Using it to limit the
  inventory read is a possible follow-up.
- **Comments:** notes are re-read only when an item's `updated_at` or
  `user_notes_count` changed. GitLab bumps `updated_at` when a comment is added
  or edited, at most once a minute (covered by the overlap), but not when one is
  deleted, which only lowers the count. Items whose two fields did not change
  are not re-rendered, so a project rename reaches their text only when they next change.
- **Wiki:** the listing endpoint has no `updated_after` filter and no pagination,
  so every run reads all pages with content and compares hashes.
- **Deletions:** a known item missing from the inventory is read directly. Only a
  404 emits a `_deleted` hard-delete marker; a successful read keeps it, which
  protects against offset-pagination skips. GitLab also returns 404 for items the
  token can no longer see, so this removes items from the token's visible scope,
  not only physically deleted ones. Projects missing from discovery are checked
  with `GET /projects/:id` the same way.
- **Failures:** 401, 429, 5xx, timeouts, redirects, failed pages, and malformed
  payloads abort the run without saving a partial checkpoint or emitting
  tombstones. There are no automatic retries; a 429 raises a rate-limit error
  that includes GitLab's `Retry-After` wait when the response has one, and
  rerunning later is safe. No response bodies or tokens are logged.
- **Isolation:** staging and pipeline names are scoped by instance URL, token
  owner ID, resource selection, project selection, the `include_internal`
  setting, and destination dataset.
  Rotating the token keeps the same scope.
- **Memory:** each scan buffers documents and bookkeeping in memory; very large
  inventories would need a database-backed state strategy.

The saved checkpoint is a staging checkpoint. A successful dlt load does not
guarantee Cognee's graph processing finished; check the result's `status`.

## Cognee version

The package pins `cognee==1.6.5`, the same version as the other storage and
document connectors under review. Cognee 1.4.0 skipped graph cleanup when a
dataset's last document was deleted. Deleting the final document of a dataset has
not been verified live with this connector; the live deletion check below removed
one issue while others remained.

## Live validation

Run read-only against gitlab.com with DeepSeek as the LLM and local fastembed
embeddings:

- Membership discovery found the account's 3 projects, including a group project.
  The first sync produced 36 documents (29 with comments); an unchanged second sync
  emitted 0 rows and made 0 notes requests.
- Comment timestamps, on a throwaway issue:

  | Action | Parent `updated_at` | `user_notes_count` |
  | --- | --- | --- |
  | Add (after 70s) | bumped | 0 → 1 |
  | Add (within 60s) | unchanged | 1 → 2 |
  | Edit (after 70s) | bumped | 2 |
  | Delete (after 70s) | **unchanged** | 2 → 1 |

- End to end (`remember` → delete the issue upstream → `remember`), on an isolated
  Cognee data directory with a throwaway issue carrying a unique marker:

  | | Data rows with marker | Chunk hits | Graph nodes with marker | Graph nodes / edges |
  | --- | --- | --- | --- | --- |
  | After sync 1 | 1 | 1 | 4 | 59 / 113 |
  | After deleting the issue and sync 2 | **0** | **0** | **0** | 52 / 100 |

## Design notes for maintainers

| Module | Role |
| --- | --- |
| `gitlab.py` | HTTP helpers, token validation, project discovery, list/notes/wiki fetchers, and `gitlab_source()`. |
| `sync.py` | `sync_documents()`: cursors, inventories, note re-reads, hashing, deletion checks, and the next state. |
| `documents.py` | Pure mappers from GitLab payloads to `{id, title, content, url}`; no HTTP. |

Invariants to keep when changing the sync:

- Never decide a deletion from the `updated_after` delta; use the full inventory
  plus a direct read that returns 404.
- Build the next state separately and assign it only after every read and mapping
  succeeds, so a failure never publishes a partial checkpoint or tombstones.
- Never follow redirects or server-supplied pagination URLs, so the token stays on
  the configured instance.
- Keep document IDs stable; they are the merge and hard-delete key:
  `<instance>:project:<project_id>:issue:<id>`, `...:merge_request:<id>`, and
  `...:wiki:<url-encoded slug>`. A wiki rename is a delete plus an add.

Stored state: `resource_state()["projects"][<project_id>][<resource>]` holds
`hashes` (document ID → content hash), `identities` (document ID → IID or slug),
`updated_after` (issues/MRs), and `notes_marks` (document ID →
`[updated_at, user_notes_count]`, comment mode only). Changing this shape breaks
existing users' saved state, so treat it as a migration.

## Tests

```bash
uv run pytest tests/
uv run ruff check .
uv run ruff format --check .
```

190 offline tests cover token validation, discovery, mapping, comments,
internal-content filtering, rate-limit messages,
incremental cursors, deletion reconciliation, failure atomicity, token rotation,
and a real dlt + SQLite pipeline with hard deletes. They need no network or
credentials. See [`tests/README.md`](tests/README.md) for the layout.
