# cognee-community-connector-gitlab

Python connector for [cognee issue #4709](https://github.com/topoteretes/cognee/issues/4709).

**Status:** legacy PAT validation, project discovery, issue/MR/wiki fetching,
DLT document ingestion, persistent checkpoints, and resource deletion reconciliation
are implemented. Comments remain unimplemented. `gitlab_source()` validates the
PAT immediately; fetching and mapping happen during DLT extraction.

This folder is staged in the core cognee checkout for development. Its intended
contribution location is `packages/connector/gitlab/` in cognee-community.

## Planned scope

- Required legacy Personal Access Token with `read_api`, supplied through `token`.
  The broader `api` scope also passes validation but is not needed.
- Configurable `base_url`, defaulting to `https://gitlab.com`.
- Required `resource_types`: `issues`, `merge_requests`, `comments`, and `wiki`.
  An empty selection is rejected.
  Unsupported names are rejected before any HTTP requests.
- Comments on issues and merge requests are reserved for a later implementation;
  selecting `comments` currently raises before any HTTP requests.
- Document rows with `id`, `title`, `content`, and `url`, exposed as a `dlt` source
  for `cognee.remember()`.
- Incremental updates and verified upstream deletion reconciliation.
- Python API only; no UI in this package.

Token validation uses `GET /api/v4/personal_access_tokens/self` and checks active
status and read scope. GitLab's active status already accounts for expiry and
revocation; the connector does not compare dates against the local clock.
Discovery uses keyset pagination on `GET /api/v4/projects` to list all accessible projects, including
public projects. There is no mandatory `project_ids` parameter. HTTP failures,
redirects, timeouts, and malformed responses abort the operation rather than
being interpreted as empty results. No response bodies or tokens are logged.

When `issues` is selected, the connector reads `/projects/:id/issues` for each
discovered project, with `scope=all`, `state=all`, and offset pagination. It
maps the issue payloads into documents. A failed page aborts the operation.
Later runs apply `updated_after` from the saved per-project issue checkpoint.

When `merge_requests` is selected, the connector reads `/projects/:id/merge_requests`
with `scope=all`, `state=all`, and offset pagination, including closed and merged
MRs. Issues and MRs share pagination and identity validation helpers. Later MR
fetches also use their own `updated_after` checkpoint.

When `wiki` is selected, the connector reads `/projects/:id/wikis?with_content=1`.
GitLab returns the entire wiki page list without pagination. Pages are identified
by their unique slug and include raw content and format. Empty content is valid;
missing content is an error. HTTP 403/404 is not treated as an empty wiki.

## Resource payloads and document mapping

All mappers return `{id, title, content, url}`. They do not make HTTP requests.
Useful metadata is rendered into `content`, which is the text cognee processes.

| Resource | Required payload fields | Optional fields consumed | Mapping |
| --- | --- | --- | --- |
| Issue | `id`, `iid`, `project_id`, `title` | `description`, `state`, `labels`, `author`, `web_url` | Title, description, project, reference, state, author, labels, source URL |
| Merge request | `id`, `iid`, `project_id`, `title` | Issue fields plus `source_branch`, `target_branch` | Same prose format, with MR reference and branch metadata |
| Wiki | `slug`, `title`, `content` | `format` | Title and original markup, project, format, source URL |

Each mapper also receives the project and GitLab instance URL. Projects need an
`id`, and `web_url` or `path_with_namespace` when constructing URLs. Issue/MR
`web_url` takes precedence when supplied. Wiki source URLs are constructed from
the project URL and encoded slug.

Document IDs include the instance, project, type, and upstream ID (wiki slug).
Edits keep the same external ID; a wiki rename changes it. Titles and content
changes do not affect external identity. Null/missing descriptions become empty
text; absent optional metadata is omitted. Wiki markup is preserved, not
converted from AsciiDoc/RDoc to Markdown. Unknown API fields are not ingested.

Comments, MR commits, diffs, attachments, repository README files, and `docs/`
files are not fetched or included. Comment counts are not comment content.
This package maps GitLab payloads only; GitHub has no adapter here.

## Ingest into cognee

```python
import os
import cognee
from cognee_community_connector_gitlab import gitlab_source

source = gitlab_source(
    token=os.environ["GITLAB_TOKEN"],
    resource_types=["issues", "merge_requests", "wiki"],
    base_url="https://gitlab.com",
)
await cognee.remember(
    source,
    dataset_name="gitlab",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
    run_in_background=False,
)
```

Use a dedicated dataset and explicitly pass `write_disposition="merge"` because
cognee's default is `replace`. The source declares document mode so each row is
processed as prose, rather than a relational schema manifest. A compatible core
must provide `DOCUMENT_SYNC_VERSION >= 1`.

## Persistent sync state

`sync.py` coordinates fetching and reconciliation; `gitlab.py` contains HTTP
helpers and the DLT entry point, and `documents.py` maps resource payloads.

Inside the DLT resource, `dlt.current.resource_state()` holds per-project and
per-resource checkpoints, document hashes, and upstream identities. Full document
bodies are stored in staging, not in state. DLT persists state in its pipeline
working directory (normally `~/.dlt/pipelines/<pipeline_name>/`) and destination
`_dlt_pipeline_state` table. Keep the same destination and dataset across runs.
Staging and pipeline names are scoped by instance URL, token owner ID, resource
selection, and destination dataset. Token rotation preserves the scope.

- First run: fetch all selected resources and establish a baseline.
- Later issue/MR runs: use the greatest observed upstream `updated_at`, minus a
  one-minute overlap, as `updated_after`. Each resource has its own cursor.
  Invalid or timezone-free timestamps abort the run. Empty resources retain no
  timestamp until an upstream record supplies one.
- Wiki: fetch all pages and compare mapped-document hashes, since its listing
  endpoint has no `updated_after` filter.
- Only changed mapped documents are emitted; stable IDs and merge make replay
  idempotent. Hashing also catches changes to project metadata rendered in text.
- Deletion checks still fetch a complete unfiltered issue/MR inventory every
  run. **This implementation reduces emitted documents, not total API traffic:**
  inventories return full payloads, so a later run can make more API calls than
  a full snapshot alone. GitLab does not supply a deletion feed here.
- Previously known records absent from both inventory and delta are checked
  individually. A successful individual read retains the record (protecting
  against pagination races); HTTP 404 emits an `_deleted` hard-delete marker.
  DLT removes the staged row on merge, and Cognee reconciles the retained corpus.
  GitLab can also hide inaccessible records behind 404: this means removal from
  the token's visible resource scope, not proof of a physical upstream deletion.
- Previously known projects missing from discovery are checked with
  `GET /projects/:id`. A 200 response keeps the project in the sync; a 404 emits
  deletion markers for all its known documents and removes its saved checkpoints.
  This covers deleted projects and projects hidden after access is revoked;
  GitLab's 404 does not distinguish those cases. Cleanup runs on the next sync.
  HTTP 401/403, failed pages, malformed payloads, and mapping errors abort the
  run without publishing partial checkpoints or tombstones.

The coordinator builds the next state separately and assigns it only after all
reads, mapping, and row iteration finish. DLT owns extraction/load state and
retry handling; the checkpoint is a staging checkpoint, not a guarantee that
Cognee graph processing finished. Retained staging rows allow graph processing
to be retried even when the next extraction has no changes.

Always pass `write_disposition="merge"` and `max_rows_per_table=0` to
`cognee.remember()`: replace would erase unchanged rows, and a row limit would
truncate the corpus used for reconciliation. Recreate the source for each sync.
Current scans buffer documents and bookkeeping in memory; large inventories
will need a database-backed state strategy. Readable confidential issues are
included, so choose the destination dataset's permissions accordingly.

## Development setup

From this package directory:

```bash
uv sync --extra dev
```

Dependencies are initial development bounds; compatibility must be validated
before release. HTTP requests use `httpx` with a 30-second timeout.

## Example and tests

`examples/example.py` runs the initial ingestion flow. It requires `GITLAB_TOKEN`
and a configured cognee LLM/embedding provider. Run the mocked API and local
SQLite DLT integration tests with
`uv run pytest tests/`. These do not require credentials or network access.
