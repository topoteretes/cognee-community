# cognee-community-connector-gitbook

Sync GitBook sites and pages into [cognee](https://github.com/topoteretes/cognee)
as normal documents. Each page includes its title, path, markdown body, parent
page ID, space title, site title, and organization ID. Each site also has a
summary document with its title, URL, and visibility.

## Install and setup

Requires Python 3.11–3.13 and cognee 1.4.0's document-mode ingestion (pinned in
`pyproject.toml`), following the Metabase connector.

```bash
cd packages/connector/gitbook
pip install -e .
```

## Authentication

1. In GitBook, open **Account → Developer tools → Create new API token**.
   See [GitBook authentication](https://gitbook.com/docs/developers/gitbook-api/authentication).
2. Create a personal access token with access to the organizations and spaces
   you want to ingest.
3. Export the token and your cognee LLM credentials:

   ```bash
   export GITBOOK_API_TOKEN="..."
   export GITBOOK_ORG_ID="..."  # optional: otherwise discover accessible organizations
   export LLM_API_KEY="..."
   python examples/example.py
   ```

The connector sends the token only as `Authorization: Bearer <token>`, never
as a URL parameter or log message. Base URLs containing credentials or query
parameters are rejected. HTTP redirects are not followed, and pagination links
must stay on the same origin and endpoint.

## Usage

```python
import cognee
from cognee_community_connector_gitbook import gitbook_source

await cognee.remember(
    gitbook_source(),  # configuration from environment
    dataset_name="gitbook",
)
```

Arguments override environment values. Use a dedicated cognee dataset for each
API instance and organization selection; keep the same pipeline storage between
runs. This example sends document content through your configured cognee
ingestion/LLM pipeline.

## Configuration

| Argument | Environment fallback | Default / meaning |
| --- | --- | --- |
| `api_token` | `GITBOOK_API_TOKEN` | Required personal access token |
| `org_id` | `GITBOOK_ORG_ID` | Discover all accessible organizations when omitted |
| `base_url` | — | `https://api.gitbook.com`; override for tests |
| `client` | — | Optional caller-owned `httpx.Client` |
| `request_interval` | — | 0.1 seconds between requests |

## Snapshot sync and deletion

Every run lists all selected organizations, sites, and linked spaces, then reads
the current space page trees and each document page's markdown. Staging uses
`write_disposition="replace"` and cognee's document-source marker, exactly as the
Metabase connector. Stable IDs and content hashes preserve unchanged documents.
Pages removed from a tree, spaces unlinked from a site, and deleted sites disappear
from staging; cognee's deferred `orphan_cleanup` forgets their graph/vector
records. No custom deletion callback or separate incremental cursor is needed.

A stable **GitBook source** document remains even when every upstream item is
deleted: cognee 1.4.0 skips cleanup for completely empty snapshots. This allows
the final deletion to use the same cleanup path.

All reads finish before a snapshot is yielded. Authentication, malformed JSON,
invalid response structures, and other request failures abort extraction without
replacing staging. `GitBookAuthError` reports 401; `GitBookResponseError` reports
invalid JSON/structure/pagination; other HTTP failures raise `GitBookError`.
dlt and cognee wrap these errors in their extraction exceptions.

A 404 logs a warning and skips the missing resource. A 429 respects `Retry-After`
(seconds or HTTP date) once, then fails if the retry fails. Missing or invalid
`Retry-After` defaults to one second. Requests are sequential and paced.

## Scope and limitations

Only spaces linked to accessible sites are ingested. The connector reads the
current published revision of each space, not change-request drafts or revision
history. It includes hidden pages returned by the API. Groups, links, and computed
page placeholders retain their metadata; only document pages have markdown
bodies. Empty document pages have an empty body. Linked URLs and attachments are
not downloaded, and computed content is not expanded.

A page shared by multiple sites has one document per site context so its site
provenance stays accurate. The full snapshot is held in memory before loading.
Upstream changes during a run are not transactionally isolated; a later run
reconciles them. Lost access or a skipped 404 removes the corresponding documents
from the authoritative snapshot, just like deletion.

## API notes

Verified against GitBook's [official API reference](https://gitbook.com/docs/developers/gitbook-api/api-reference)
and its linked [OpenAPI specification](https://api.gitbook.com/openapi.json).
The specification uses `https://api.gitbook.com/v1` as the server base.

| Request | OpenAPI operation | Response fields used |
| --- | --- | --- |
| `GET /v1/orgs` | `listOrganizationsForAuthenticatedUser` | `items[].id`, `next.page` |
| `GET /v1/orgs/{organizationId}/sites` | `listSites` | `items[].id`, `title`, `visibility`, `urls`, `next.page` |
| `GET /v1/orgs/{organizationId}/sites/{siteId}/site-spaces` | `listSiteSpaces` | `items[].space.id`, `next.page` |
| `GET /v1/spaces/{spaceId}` | `getSpaceById` | `title` |
| `GET /v1/spaces/{spaceId}/content` | `getCurrentRevision` | `pages`, recursively nested `pages` |
| `GET /v1/spaces/{spaceId}/content/page/{pageId}?format=markdown` | `getPageById` | `title`, `path`, `markdown` |

**Difference from the task's proposed API:** `GET /v1/sites/{siteId}` is not the
site-space discovery endpoint in the official specification. `Site.siteSpaces`
is a numeric count, not a list of IDs. The organization-scoped `listSiteSpaces`
endpoint returns `SiteSpace` objects containing a nested `space` object. This
connector follows that endpoint and fetches all its pages.

Lists use `items` and an optional `next.page` cursor, sent back as the `page`
query parameter until exhausted. Compatible `Link: ...; rel="next"` pagination
is also followed, allowing `page`, `all`, and `limit` parameters. The official
listing operations do not require an `all` parameter. Repeated cursors fail
explicitly. Page trees themselves are complete revision responses, not paginated
lists. Markdown is a top-level `markdown` string, not `document.markdown`.

## Testing

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests follow Metabase's fixture-based `httpx.MockTransport` and temporary SQLite
dlt pipeline conventions. Fixtures contain minimal JSON projections of the
verified API schemas; they are not recordings from a live private account.
Sockets and DNS are blocked in every test. Tests exercise cognee's real document
resolver and orphan cleanup with only graph/persistence boundaries mocked, so
no live GitBook server, LLM credentials, or external network is required.
