# cognee-community-connector-bitbucket

A Bitbucket Cloud data-source connector for [cognee](https://github.com/topoteretes/cognee).
It ingests selected pull requests, pull request comments, and repository wiki pages as
documents through cognee's existing DLT ingestion path.

This first version supports **Bitbucket Cloud only**. Bitbucket Server and Data Center use
different APIs and are not supported.

## Install

```bash
uv pip install cognee-community-connector-bitbucket
# or, from this monorepo:
cd packages/connector/bitbucket && uv sync
```

## Authentication

The connector accepts either an OAuth 2.0 bearer access token or an Atlassian API token.
For OAuth, pass an access token obtained by your application's OAuth flow. For an API token,
pass the Atlassian account email and token. The connector does not run an interactive OAuth
authorization flow or persist credentials.

Create an API token with **Repositories: Read** and **Pull requests: Read**; add **Wikis: Read**
when `wiki` is selected. The documented API-token scope names for the first two permissions are
`read:repository:bitbucket` and `read:pullrequest:bitbucket`. Atlassian lists wiki permissions
separately but does not give them an equivalent scope name on that page. For OAuth, grant
`repository` and `pullrequest`; Bitbucket also has a `wiki` scope for standalone wiki access,
which includes write access because it does not offer a read-only wiki scope. The connector uses
OAuth bearer authentication or Basic authentication with the Atlassian email and API token.
Atlassian stopped allowing new app passwords on September 9, 2025 and disabled existing app
passwords on June 9, 2026 ([Atlassian's app-password end-of-life notice](https://support.atlassian.com/bitbucket-cloud/docs/revoke-an-app-password/)).
Use an API token or OAuth instead; this package does not support app passwords.

You can pass credentials directly or set environment variables:

```bash
export BITBUCKET_ACCESS_TOKEN="<OAuth access token>"
# Or use an Atlassian API token:
export BITBUCKET_EMAIL="you@example.com"
export BITBUCKET_API_TOKEN="<Atlassian API token>"
export LLM_API_KEY="<your cognee model provider key>"

# Select the Bitbucket Cloud workspace and repository slugs to sync.
export BITBUCKET_WORKSPACE="my-workspace"
export BITBUCKET_REPOSITORIES="service-api,web-app"
```

For OAuth and API-token setup details, see Atlassian's [authentication documentation](https://developer.atlassian.com/cloud/bitbucket/rest/intro/#authentication)
and [API token permissions](https://support.atlassian.com/bitbucket-cloud/docs/api-token-permissions/).

Use the narrowest permissions available for your workspace and repositories. Never put a
token in a repository URL or commit it to source control.

## Usage

```python
import cognee
from cognee_community_connector_bitbucket import bitbucket_source

source = bitbucket_source(
    workspace="my-workspace",
    repositories=["service-api", "web-app"],
    content_types=["pull_requests", "comments", "wiki"],
)

await cognee.remember(
    source,
    dataset_name="bitbucket",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)

answer = await cognee.search(
    query_text="What changed in the recent pull requests?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["bitbucket"],
)
print(answer)
```

`content_types` accepts any combination of `pull_requests`, `comments`, and `wiki`. The
`access_token` argument uses OAuth bearer authentication; `api_token` with `email` uses
Atlassian API-token authentication. See [examples/example.py](examples/example.py) for a
runnable sync example. From this package directory, run it with:

```bash
uv sync
uv run python examples/example.py
```

## Sync and deletion behavior

Pull requests use the Bitbucket `updated_on` filter and a per-repository cursor stored in DLT
resource state. The connector inventories pull requests, comments, and wiki files on each run
to detect deletions; comment and wiki content is also read to detect edits, but only changed
documents are emitted. Stable IDs include the workspace/repository and PR number, comment ID,
or wiki page path. Upstream deletions are emitted as DLT hard-delete rows only after the
complete authoritative inventory for the affected workspace, repository, and content type has
been fetched. Cognee's existing `orphan_cleanup` then removes forgotten documents from the graph
and vector stores. An API fetch failure raises an error and does not advance sync state or
produce deletion rows.
Bitbucket may omit `has_wiki` from repository metadata. When that happens, PRs and comments still
sync, but the previous wiki inventory is preserved without wiki deletion rows until wiki status
can be established. A present but non-boolean `has_wiki` value is malformed and fails the sync
closed.

Pagination follows Bitbucket's opaque `next` links. Rate limits and transient server/network
errors are retried with `Retry-After` support. Invalid/expired credentials, missing access,
and exhausted rate limits produce credential-safe error messages.

## Testing

```bash
uv run pytest tests/
```

The tests mock Bitbucket Cloud responses and require no live workspace or credentials.

## References

- [Bitbucket Cloud REST API: authentication and scopes](https://developer.atlassian.com/cloud/bitbucket/rest/intro/#authentication)
- [Bitbucket Cloud REST API: pull requests](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/)
- [Bitbucket Cloud REST API: source files](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-source/)
- [Atlassian support: using API tokens](https://support.atlassian.com/bitbucket-cloud/docs/using-api-tokens/)
- [Atlassian support: API token permissions](https://support.atlassian.com/bitbucket-cloud/docs/api-token-permissions/)
