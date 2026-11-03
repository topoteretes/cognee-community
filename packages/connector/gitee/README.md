# Cognee Connector: Gitee

A data-source connector for [Gitee](https://gitee.com) (China's leading open-source code hosting platform). Ingests repositories, issues, pull requests, commits, and releases into Cognee's knowledge graph and vector store.

Resolves: [`topoteretes/cognee#6364`](https://github.com/topoteretes/cognee/issues/6364).

---

## Capabilities

- **Repository Metadata:** Description, default branch, stars, forks, and repository statistics.
- **Issue Tracking:** Issue title, author, state, full body markdown, timestamps, and direct URLs.
- **Pull Requests (Merge Requests):** PR title, branch relationships, mergeability status, summaries, and discussions.
- **Commit History:** SHA hash, author metadata, commit message, and timeline.
- **Releases:** Tags, author, published date, and release notes.
- **Incremental Sync:** Advances via `since` cursor so subsequent runs ingest only newly updated or created entities.
- **Forget-On-Delete:** Emits documents with deterministic content-hash `data_id` and `write_disposition="replace"` for seamless orphan cleanup.

---

## Authentication

Create a free Gitee Personal Access Token:
1. Log in to [gitee.com](https://gitee.com).
2. Go to **Settings** (`个人设置`) -> **Private Token** (`私人令牌`).
3. Generate a token with the `projects` (Read) permission.
4. Export the token in your environment:
   ```bash
   export GITEE_ACCESS_TOKEN="your_personal_access_token_here"
   ```

*Note: This connector targets public and authenticated open-source `gitee.com` (API v5). Gitee Enterprise private cloud deployments use custom endpoints and are out of scope.*

---

## Quickstart

```python
import os
from cognee_community_connector_gitee import GiteeConnector

# Initialize connector
connector = GiteeConnector(access_token=os.getenv("GITEE_ACCESS_TOKEN"))

# Ingest all repository data
documents = connector.fetch_repository(
    owner="oschina",
    repo="git-osc",
    include_issues=True,
    include_pulls=True,
    include_commits=True,
    include_releases=True,
    max_pages=3
)

print(f"Ingested {len(documents)} Cognee documents from Gitee!")
```

---

## Running Tests

```bash
PYTHONPATH=. pytest packages/connector/gitee/tests -v
```
