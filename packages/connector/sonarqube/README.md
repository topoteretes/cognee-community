# cognee-community-connector-sonarqube

A [cognee](https://github.com/topoteretes/cognee) data-source connector for
[SonarQube](https://www.sonarsource.com/) (Server, Community Build, or Cloud):
syncs issues, Security Hotspots and quality-gate outcomes into cognee's memory.

Issues, Security Hotspots and quality-gate outcomes are all ingested
incrementally: a full re-list every run, compared by content hash, so
unchanged documents produce no writes, changed ones re-emit, and documents
that drop out of the filtered listing (fixed, resolved, reassigned, reviewed,
or genuinely deleted) are forgotten via a hard-delete tombstone. A mass-delete
guard with a spot-check protects against mistaking a lost permission for a
wave of real deletions (see "Guards" below).

**Nothing in this package has been run against a real SonarQube instance.**
It is built and tested entirely against `tests/fake_sonarqube.py` (a
hand-rolled fake of the SonarQube Web API), a real `dlt` pipeline merging into
a temp sqlite database, and a real `cognee.cognify()` run with the LLM and
embeddings mocked -- see "Testing" below for exactly what that does and
doesn't prove.

## Setup

1. Generate a SonarQube **user token**: in SonarQube, go to **My Account >
   Security > Generate Tokens**, pick the "User" token type, give it a name,
   and copy the value once -- it is never shown again. The token needs at
   least **Browse** permission on every project you select (and inherits
   whatever else that user account can see). On SonarQube Cloud this is under
   your account's **Security** settings instead of a separate "My Account"
   page, but the token type and flow are the same.
2. Set it as an environment variable (or pass it directly):

   ```bash
   export SONARQUBE_TOKEN=squ_...
   export SONARQUBE_URL=https://sonarqube.example.com   # or https://sonarcloud.io
   export SONARQUBE_PROJECTS=my_project,my_other_project
   ```

   On SonarQube Cloud, also set `SONARQUBE_ORGANIZATION` to your organization key.

## SonarQube Server/Community Build vs. Cloud

The connector talks to both through the same Web API shape, with two
differences it handles for you:

* **Base URL**: Server/Community Build is self-hosted (`SONARQUBE_URL` is
  whatever host you run it at, or `http://localhost:9000` for a local Docker
  instance); Cloud is always `https://sonarcloud.io`.
* **Organization**: Cloud scopes everything to an `organization` key, which
  this connector requires (and sends on every request) whenever the host is
  `sonarcloud.io`. Server/Community Build has no such concept and the
  parameter is never sent.

Auth is identical either way: a user token sent as `Authorization: Bearer
<token>` by default (SonarSource's documented, recommended scheme for both).
Pass `auth_scheme="basic"` to send it as HTTP Basic instead (the token as the
username, empty password) -- also documented, for hosts/proxies that don't
support Bearer.

## Selecting what to sync

Pass `project_keys=[...]` for an explicit list, or `all_projects=True` to sync
every project the token can Browse (an explicit opt-in -- it can be a lot of
data). With `project_keys`, removing a key from the list forgets that
project's documents on the next run without any request; with `all_projects`,
a project that silently drops out of the listing is verified by id before
being forgotten (see "Guards" below -- the same lost-permission-vs-deletion
concern applies at the project level, not just per-document).

`branch` scopes every issues/hotspots/quality-gate call to one branch; omitted,
SonarQube uses the project's main branch. Document ids and sync state are
scoped by branch too, so running the same project under two different
`branch` values (e.g. `main` and a feature branch) keeps two independent sets
of documents -- syncing one branch never tombstones the other's.

## Usage

```python
import cognee
from cognee_community_connector_sonarqube import sonarqube_source

await cognee.remember(
    sonarqube_source(project_keys=["my_project"]),
    dataset_name="sonarqube",
    # Required: the dlt resource declares write_disposition="merge" and
    # primary_key="id", but cognee.remember()/add() read these from their own
    # kwargs, not from the resource -- pass them explicitly or every run
    # replaces the whole dataset instead of upserting/deleting by id.
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```

## What is ingested

* **Issues** -- unresolved issues (`resolved=false`) at or above a severity
  threshold (default `MAJOR, CRITICAL, BLOCKER`) of the selected types
  (default `CODE_SMELL, BUG, VULNERABILITY`). A large project can have tens of
  thousands of issues; the severity threshold and `max_issues_per_project`
  (default 5000) keep a sync bounded -- the connector raises rather than
  silently truncating if a project has more matching issues than the cap.
* **Security Hotspots** -- hotspots with status `TO_REVIEW` (the default scope:
  once a hotspot is reviewed -- `SAFE`, `FIXED` or `ACKNOWLEDGED` -- it drops
  out and is forgotten, the same way a fixed issue is). `max_hotspots_per_project`
  (default 1000) bounds the sync the same way.
* **Quality gate** -- the project's current gate status (`OK`/`WARN`/`ERROR`)
  and its failing/warning conditions. Skipped entirely when the status is
  `NONE` (the project has never been analyzed) -- there is nothing to say yet.

Why these defaults: SonarQube issue volume on a large project is routinely in
the tens of thousands, and most of it is noise for a memory layer -- the
severity threshold keeps only what's worth remembering, and the per-project
caps turn "silently truncated and later looks deleted" into a loud, actionable
error instead.

## Why `created_after` is a scope filter, not a change detector

SonarQube's issue search has no `updatedAfter`-style parameter: `createdAfter`
only narrows by *creation* time, so it can never see a status change, a
resolution, a reassignment or a re-rated severity on an issue created before
the cursor (confirmed against the live Web API reference -- see the research
notes). `created_after` here is therefore only ever a scope filter you choose
once (e.g. "ignore issues from before our rollout"); it is not how changes are
detected. Every run re-lists every unresolved, in-scope issue and decides what
changed with a content hash (title + content + url) -- that is the real
detection mechanism, and it is why a fixed/resolved/reassigned issue is
forgotten exactly like a deleted one (SonarQube answers both the same way:
silent omission from the listing, never a 404).

## Guards against mistaking a lost permission for a deletion

Because a lost permission and a real bulk deletion look identical in
SonarQube's responses (both just stop appearing in the listing), a pass that
would forget an unusually large share of a project's previously-synced
documents -- more than half, and at least 5 -- or that would forget *every*
document in a project that previously had some, does not delete blindly. It
spot-checks up to 5 of the vanished issues/hotspots directly -- issues via
`issues/search?issues=<key>` (no resolved/severity/type filter), hotspots via
`hotspots/show` -- to see whether each still matches the current filters:

* Gone, or no longer matching (resolved, reassigned, below the severity
  threshold, wrong type, reviewed) -- confirms the deletion is real; a
  legitimately all-fixed/all-reviewed project is allowed through.
* Still matching -- the listing itself looks unreliable (most likely a lost
  permission on the listing endpoint specifically). The whole sync raises
  and deletes nothing, leaving state untouched.

The quality-gate document is exempt from spot-checking: it's a single document
per project whose presence comes straight from that run's own fresh
`qualitygates/project_status` call, so there's no silent-omission ambiguity to
resolve (unlike issues/hotspots, which are only known to have vanished because
a *listing* stopped including them).

Pass `allow_mass_delete=True` to skip the guard entirely (e.g. for a project
you know was deliberately decommissioned).

## The 10,000-result search window

SonarQube's issue and hotspot search is Elasticsearch-backed and enforces a
hard `page * page_size <= 10,000` ceiling -- past it the API itself refuses
the request. `max_issues_per_project` (default 5000) and
`max_hotspots_per_project` (default 1000) are deliberately well under that
ceiling and are enforced by this connector *before* a listing gets anywhere
near it: a project with more matching issues/hotspots than the cap makes the
whole sync raise, naming the project and the limit, rather than silently
returning a truncated page that would later look like a wave of deletions.
Raise the cap, narrow `issue_types`, raise the severity threshold, or set
`created_after` to bring a project back under it.

## Parameters

See the docstring of `sonarqube_source` in
`cognee_community_connector_sonarqube/sonarqube.py` for the full parameter
list, defaults and env var fallbacks (`SONARQUBE_TOKEN`, `SONARQUBE_URL`,
`SONARQUBE_ORGANIZATION`, `SONARQUBE_PROJECTS`).

## Privacy

Issue messages and file paths can reveal source code structure and content.
The issue author is **not** included by default (`include_author=False`);
set it to `True` only if that is acceptable for your use case.

## Testing

```bash
uv sync --all-extras
uv run pytest tests/ -v
```

Three layers, all offline:

* `tests/fake_sonarqube.py`, a hand-rolled fake of the SonarQube Web API
  (no `respx`/`mock` on HTTP) -- covers validation, the retrying client,
  rendering, the hash gate, tombstones, guards/spot-checks and project
  handling against scripted responses.
* A real `dlt` pipeline merging into a temp sqlite database -- proves the
  `_deleted` hard-delete marker actually removes rows at the dlt layer, not
  just that this connector intends it to.
  (`tests/test_sonarqube_pipeline.py`)
* A real `cognee.cognify()` run with the LLM and embeddings mocked -- proves
  a resolved issue's entity actually leaves the graph, not just that a
  tombstone row was emitted. (`tests/test_sonarqube_forget.py`)

**What this does *not* prove**: that any of the SonarQube Web API's exact
request/response shapes used here (parameter names, pagination fields, error
bodies) match a real, running SonarQube Server, Community Build or Cloud
instance. Those were taken from SonarSource's own live Web API reference
documentation and corroborating community reports, not exercised against a
real server -- see the research notes for what's confirmed vs. UNVERIFIED,
and test against a real instance before relying on this in production.
