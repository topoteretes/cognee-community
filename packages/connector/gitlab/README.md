# cognee-community-connector-gitlab

Python connector scaffold for [cognee issue #4709](https://github.com/topoteretes/cognee/issues/4709).

**Status:** PAT validation, paginated project discovery, issue and merge request
fetching, and wiki page fetching are implemented.
`gitlab_source()` performs these reads, then raises `NotImplementedError` because
document rendering, comments, ingestion, and sync are not implemented yet.

This folder is staged in the core cognee checkout for development. Its intended
contribution location is `packages/connector/gitlab/` in cognee-community.

## Planned scope

- Required legacy Personal Access Token with `read_api`, supplied through `token`.
  The broader `api` scope also passes validation but is not needed.
- Configurable `base_url`, defaulting to `https://gitlab.com`.
- Required `resource_types`: `issues`, `merge_requests`, `comments`, and `wiki`.
  An empty selection is rejected.
  Unsupported names are rejected before any HTTP requests.
- Comments on issues and merge requests.
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
preserves the raw issue payloads; rendering and delivering them to cognee is a
later step. A failed page aborts the operation. This is a full fetch, not yet an
incremental sync or a deletion inventory safe against concurrent upstream edits.

When `merge_requests` is selected, the connector reads `/projects/:id/merge_requests`
with `scope=all`, `state=all`, and offset pagination, including closed and merged
MRs. Issues and MRs share pagination and identity validation helpers.

When `wiki` is selected, the connector reads `/projects/:id/wikis?with_content=1`.
GitLab returns the entire wiki page list without pagination. Pages are identified
by their unique slug and include raw content and format. Empty content is valid;
missing content is an error. HTTP 403/404 is not treated as an empty wiki.

## Development setup

From this package directory:

```bash
uv sync --extra dev
```

Dependencies are initial development bounds; compatibility must be validated
before release. HTTP requests use `httpx` with a 30-second timeout.

## Example and tests

`examples/example.py` shows the intended calling shape. It will become runnable
after the connector is implemented. Run the current mocked API tests with
`uv run pytest tests/`. These do not require credentials or network access.
