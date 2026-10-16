# cognee-community-connector-gitlab

Python connector for [cognee issue #4709](https://github.com/topoteretes/cognee/issues/4709).

**Status:** PAT validation, paginated project discovery, issue and merge request
fetching, wiki page fetching, and document-mode DLT integration are implemented.
`gitlab_source()` validates the token, fetches selected resources, maps them using
`documents.py`, and returns a source for `cognee.remember()`. Comments, incremental
fetching, and upstream deletion reconciliation remain unimplemented.

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
This is a full fetch, not yet an
incremental sync or a deletion inventory safe against concurrent upstream edits.

When `merge_requests` is selected, the connector reads `/projects/:id/merge_requests`
with `scope=all`, `state=all`, and offset pagination, including closed and merged
MRs. Issues and MRs share pagination and identity validation helpers.

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
    run_in_background=False,
)
```

Use a dedicated dataset and explicitly pass `write_disposition="merge"` because
cognee's default is `replace`. The source declares document mode so each row is
processed as prose, rather than a relational schema manifest. A compatible core
must provide `DOCUMENT_SYNC_VERSION >= 1`.

Fetching and mapping complete before the source is returned. This initial
implementation buffers the documents in memory; recreate the source for each
fresh ingestion. A failed fetch or mapping aborts before publishing a source.
Staging table and pipeline names are scoped by instance URL, token owner ID, and
resource selection. Token rotation preserves these names; different users,
instances, and selections use separate staging scopes.

Merge updates known document rows without treating missing upstream records as
deletions. Removed records remain until deletion reconciliation is implemented.
This is not yet a complete sync implementation. Resource visibility filtering
also remains pending: currently readable confidential issues are included, so
choose the destination dataset's permissions accordingly.

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
