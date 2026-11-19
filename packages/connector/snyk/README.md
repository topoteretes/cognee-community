# cognee-community-connector-snyk

A Snyk data-source connector for [cognee](https://github.com/topoteretes/cognee): read your
Snyk vulnerability findings, with their remediation advice, into memory.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each **vulnerability** becomes a
**normal document** in cognee (it flows through chunking and LLM entity extraction), via
cognee's document-mode marker. A vulnerability found in many projects is one document that
lists every affected package and project, so the same CVE is stored once and cognee links it
to the packages and projects it touches.

> **The Snyk REST API requires an Enterprise plan.** Tokens of the Free and Team plans, and
> personal tokens of trials, cannot call it: Snyk answers HTTP 403
> ([Authentication for API](https://docs.snyk.io/developer-tools/snyk-api/authentication-for-api)).
> This connector cannot work with such a token.

Re-run the same `remember` call whenever you want to sync: only vulnerabilities that changed
are processed, and vulnerabilities that are fixed, ignored or gone in Snyk are forgotten.

> **Not yet tested against a live Snyk organization.** It was written against Snyk's published
> OpenAPI specification and tested with an in-memory fake, because the REST API needs an
> Enterprise plan. Treat the first run against a real organization as a trial.

## Requirements

- cognee **1.6.3 or newer 1.x**. The connector relies on table-scoped document cleanup
  (`DOCUMENT_SYNC_VERSION`), `PIPELINE_SCOPE_ATTR` and `guarded_rows`.
- Python 3.11 to 3.13.
- A Snyk **Enterprise** plan and an API token.

## Install

```bash
uv pip install cognee-community-connector-snyk
# or, from this monorepo:
cd packages/connector/snyk && uv sync
```

## Setup

### 1. Create a read-only token

Snyk recommends a **service account** for automation (Group or Org settings > Service
accounts > Create a service account; Enterprise plans only, and not available in trials).
Give it a **Group Viewer** role (read-only for the whole group), or an organization role that
can read the organization, its projects and their issues. The permissions the connector uses:

- `org.read`, `org.project.read`, `org.project.snapshot.read` to list projects and issues
- `group.issues.read` if you read a whole group

The token is shown once. A **personal access token** (account settings > Personal access
tokens) also works on an Enterprise plan and is fine for a one-off test.

### 2. Find your region and organization

Tokens are **region specific**; a token used on the wrong region gets HTTP 401. The region is
visible in the address of your Snyk web app
([regions](https://docs.snyk.io/snyk-data-and-governance/regional-hosting-and-data-residency)):

| Region | Web app | API host used by the connector |
|---|---|---|
| `us-01` (default) | app.snyk.io | https://api.snyk.io/rest |
| `us-02` | app.us.snyk.io | https://api.us.snyk.io/rest |
| `eu-01` | app.eu.snyk.io | https://api.eu.snyk.io/rest |
| `au-01` | app.au.snyk.io | https://api.au.snyk.io/rest |

Your organization id is in Org settings. Choose what to read with `org_ids`, `group_id` (all
organizations of a group) or `all_orgs=True` (every organization the token can see). There is
no default: the connector refuses to start without one of them.

### 3. Set your cognee keys

Set `LLM_API_KEY` like any other cognee run.

## Usage

```python
import cognee
from cognee_community_connector_snyk import snyk_source

await cognee.remember(
    # SNYK_TOKEN can come from the environment
    snyk_source(org_ids=["<org id>"], region="eu-01"),
    dataset_name="snyk",
    primary_key="id",
    write_disposition="merge",  # required: matches how the resource is declared
    max_rows_per_table=0,  # no row cap; the default stops after 50 rows
)

results = await cognee.search(
    "Which critical vulnerabilities affect several projects?", datasets=["snyk"]
)
```

`remember` defaults to `write_disposition="replace"` and would override the resource's own
`merge`, so always pass it as shown. See `examples/example.py` for the full flow.

| Argument | Env var | Default |
|---|---|---|
| `api_token` | `SNYK_TOKEN` | required |
| `region` | `SNYK_REGION` | `us-01` |
| `base_url` | `SNYK_API_URL` | none; replaces the region host, https only, `/rest` is added if missing |
| `org_ids` | `SNYK_ORG_IDS` (comma-separated) | none |
| `group_id` | `SNYK_GROUP_ID` | none |
| `all_orgs` | – | `False`; an explicit opt-in |
| `project_ids` | – | every project of the selected organizations |
| `severities` | – | all (`info`, `low`, `medium`, `high`, `critical`) |
| `issue_types` | – | `("package_vulnerability",)`; `license`, `cloud`, `custom` and `config` are also accepted, `code` and `secrets` are refused |
| `include_resolved` | – | `False` |
| `include_ignored` | – | `False` |
| `include_remedies` | – | `True`: fix versions and remedy text |
| `max_projects_per_document` | – | `50`, then "and N more projects" |
| `max_issues_per_org` | – | `20000`; more raises |
| `api_version` | – | `2024-10-15` |
| `timeout` | – | `60.0` seconds per request |
| `allow_mass_delete` | – | `False`: double-check a bulk deletion against Snyk before it happens (see below) |
| `resource_name` | – | `snyk_vulnerabilities`; give each Snyk account its own name when several sync into one dataset |

After a run, `source.cognee_sync_stats` holds counts only (organizations, projects, issues,
vulnerabilities, documents emitted, unchanged, deleted and kept, spot-checks, skipped, sparse
issues), never names or findings.

## What is ingested

**One document per vulnerability per organization**, id
`snyk:<region>:<org id>:vuln:<key>`. The key is:

1. the **Snyk vulnerability id** (`SNYK-...`) found in the finding's `problems`, else
2. the first **CVE id**, else
3. no key: the finding stays a document of its own, id
   `snyk:<region>:<org id>:issue:<project id>:<issue key>`.

Two different Snyk ids that share a CVE stay two documents. A document contains the title,
the Snyk id, **every CVE and CWE id** (so cognee links the same CVE across documents), the
severity with the highest CVSS score and vector, the exploit maturity, the organization, a
clipped description, the affected packages and versions per project (sorted, with the
manifest file, the status and the kind of fix: upgrade, patch, pin, manual or none), the
remediation (fix versions, vulnerable ranges, up to five remedy texts), and sentences that
link the vulnerability to its packages, projects and fix versions.

**Never rendered** (they change without the finding changing): every timestamp, risk scores
and factors, test executions, counters.

**Privacy defaults**

- Package vulnerabilities only; no `code` or `secrets` findings (they contain source
  locations); code flows are never requested.
- Open findings that are not ignored; resolved and ignored ones are opt-in.
- No ignore reasons: Snyk's REST API does not expose them.
- The token is sent only in the `Authorization` header. It is never logged, stored in state
  or shown in an error message.

## Reused from cognee

- **`DOCUMENT_SOURCE_ATTR`**: marks the resource so each row becomes a text document that goes
  through normal cognify.
- **`PIPELINE_SCOPE_ATTR`, `DOCUMENT_SYNC_VERSION`, `guarded_rows`**: scope the dlt state per
  `resource_name`, refuse cognee builds without table-scoped cleanup, and run the optional
  `check_active` authorization checkpoint around every row. `check_active` is also called
  before every request and every wait.
- **The Argo CD, google-calendar and Asana connectors' structure**: one resource with
  `write_disposition="merge"`, `primary_key="id"` and a `_deleted` hard-delete column, rows
  with `id`/`title`/`content`, a `requests` client with retries and a run-wide wait budget.

## How sync and forget-on-delete work

- **Every run lists everything in scope.** It reads the selected organizations, their projects
  and their issues (`GET /orgs/{id}/projects`, `GET /orgs/{id}/issues`, 100 per page,
  following `links.next`), groups the issues into vulnerability documents and renders them
  all. About one request per 100 findings, plus two per organization: 50 projects and 5,000
  findings are roughly 55 requests, far below Snyk's limit of 1,620 requests per minute per
  token.
- **A content hash decides what is emitted.** The state keeps, per organization, the hash of
  every document (its title, text and url) and up to three of its issue ids. A document whose
  hash is new or different is emitted; an equal hash emits nothing, so a run where nothing
  changed emits no rows. The text holds no timestamp, risk score or counter, so only changes a
  reader would see (a new affected project, a new fix version, a changed status) count.
- **No timestamp is trusted.** `created_at`, the REST stand-in for the `introducedDate` of
  Snyk's older V1 API, never moves when a finding is fixed or ignored, so it cannot detect
  either, and nothing documents that `updated_at` moves. Listing everything and comparing
  hashes stays correct whatever the timestamps do.
- **A vulnerability is forgotten when no in-scope finding is left.** Fixed (resolved) and
  ignored findings are out of scope unless `include_resolved` / `include_ignored` is set; so
  are findings of a deleted project or one outside `project_ids`, `severities` or
  `issue_types`. A vulnerability that still has a finding in another project is re-emitted
  without the gone ones. Forgotten documents are emitted as `{"id": ..., "_deleted": True}`;
  dlt removes those rows on `merge` and cognee's orphan cleanup removes them from the graph
  and vector stores. Tombstones are yielded before the new rows, and the state is saved after
  the last row.
- **Guard and spot-check.** Snyk may silently leave out what the token cannot read, so an
  empty or shrunken listing can be a permission problem and not a real deletion. When an
  organization's listing is empty while documents are stored, or at least 5 documents and
  more than half of them would go, the connector fetches up to 5 of the findings behind
  them by id (`GET /orgs/{id}/issues/{issue_id}`):
  - 404, or a finding the current filters would not list (resolved, ignored, other severity),
    confirms the deletion;
  - a finding that still matches the filters means the listing left out something live, and
    the run raises;
  - any other answer (403, 5xx) raises.

  A raised run forgets nothing and keeps its state. So an organization where everything was
  really fixed is forgotten, while silent permission loss is refused. Smaller deletions are
  trusted without a check. `allow_mass_delete=True` skips the guard.
- **Organizations.** With an explicit `org_ids` list, an organization you removed from it is
  forgotten without any request. With `group_id` or `all_orgs=True`, an organization missing
  from the listing is fetched by id: 404 forgets it, 200 raises (the listing contradicts
  itself), any other answer keeps its documents with a warning. At most 20 organizations are
  checked per run; the rest are kept and checked next time. Documents of another region stay
  untouched.
- **Sparse list responses.** Snyk's list responses may leave out `problems` and `coordinates`
  (the spec's own list example does). The connector does not fetch each finding to complete
  it; it counts such issues in `sparse_issues` and logs one warning per run, because
  deduplication and remediation may then be incomplete.
- **The merge arguments are required.** `cognee.remember` defaults to
  `write_disposition="replace"`, which overrides the resource's `merge`. Always pass
  `primary_key="id", write_disposition="merge", max_rows_per_table=0`.
- **A failed listing aborts the whole run before anything is emitted.** HTTP 429 and 5xx
  answers are retried (five attempts, 1, 2, 4, 8 seconds or `Retry-After`; no single wait
  over 60 s, 120 s in all). If they still fail, or Snyk asks for a longer wait, the run raises
  and changes nothing. Other connectors (Asana, Apollo) stop early and keep what they have;
  this one does not, because a document combines findings from many projects, and a partial
  listing would overwrite a complete document with an incomplete one.
- An organization with more than `max_issues_per_org` matching issues raises instead of being
  cut short.
- 401 means a wrong or expired token, or a token from another region; 403 means no
  Enterprise plan or missing permissions; 404 names the organization that was not found.

## Privacy

Vulnerability data is security sensitive: it tells whoever reads it where to look.

**What is read:** from the selected organizations: project names and manifest file names,
package names and versions, vulnerability ids, titles, descriptions, severities, exploit
maturity and remediation advice. The connector only issues `GET` requests and never changes
anything in Snyk.

**Where it goes:** the text becomes cognee documents. During cognify it is sent to your
configured LLM and embedding providers, like any document you add to cognee. Use local models
if that matters to you, and restrict who can search the dataset. Project names can reveal
internal repository names.

**Logs and stats** contain counts and organization ids only.

## Limitations

- Needs an Enterprise plan (see the top of this file). The connector has been written against
  Snyk's published OpenAPI specification (versions 2024-10-15 and 2026-03-25) and has not yet
  been run against a live Snyk organization.
- Every run reads all matching issues; no timestamp filter is used, because none is known to
  move when a finding is fixed or ignored.
- Revoked access and a wrong selection look like deletion; the guard only catches bulk cases.
- A vulnerability whose findings do not carry `problems` stays a document per finding.
- Changing a setting that changes the text (for example `include_remedies`) re-emits the
  affected documents; a change of region keys documents under the new region and leaves the
  old region's documents alone.
- Ignore reasons and risk scores are not ingested.
- `full_sync_every`, an `updated_after` delta mode and per-finding detail fetches are not
  implemented.

## Testing

```bash
uv run pytest tests/
```

No Snyk account and no API key are needed. `tests/fake_snyk.py` is an in-memory stand-in for
the Snyk REST API with the same `get(url, params=..., headers=..., timeout=...)` shape as a
`requests` session: organizations, groups, projects and issues, cursor pagination through
`links.next` (absolute, relative and `{"href": ...}` links), scripted status codes and JSON:API
error bodies, and a call log.

- `test_snyk_source.py` covers the factory checks (every invalid setting, regions and hosts,
  environment fallbacks, no token in messages or logs), the HTTP client (retries,
  `Retry-After`, the wait cap and budget, 401/403/404 messages, connection hints, headers,
  `check_active`), pagination, organization and project selection, the request filters and
  the cognee markers.
- `test_snyk_sync.py` covers the grouping of findings into vulnerability documents, a golden
  document, stable output for shuffled input, volatile fields, remedies, clipping, status
  filters, limits and empty organizations.
  It also drives `_iter_rows` with a plain dict as state through the incremental cases:
  unchanged runs, new/fixed/ignored/deleted findings and projects, remediation and volatile
  changes, tombstones, the guard and its spot-checks, `allow_mass_delete`, deselected and
  vanished organizations, failed requests that leave the state byte-identical, and a state
  without secrets that survives JSON.
- `test_snyk_ingest.py` runs a real `dlt.pipeline` into SQLite: first sync, repeat runs that
  emit nothing, changed and fixed findings, tombstoned rows that are physically gone, refused
  deletions and failing APIs leaving the table unchanged.
- `test_snyk_forget.py` runs the real cognee pipeline (LLM and embeddings mocked) and checks
  that a vulnerability fixed in Snyk leaves the graph, a run with no new rows forgets nothing,
  and the others stay.

If pytest ends with `FileNotFoundError` while the repository sits on a Windows drive mounted
in WSL (`/mnt/d/...`), point `TMPDIR` at a Linux directory such as `/dev/shm/snyk-tests`.
