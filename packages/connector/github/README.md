# GitHub Connector for Cognee

Sync your GitHub repositories into Cognee memory with full-snapshot sync and forget-on-delete.

## What this connector does

- Ingests GitHub repos, issues, pull requests, commits, and releases as markdown documents
- Optionally ingests Dependabot alerts, including current state and recorded resolution details
- Stable IDs (`issue:owner/repo#42`, `commit:owner/repo#sha`) mean unchanged rows are not re-cognified
- `write_disposition="replace"` gives full-snapshot sync; deletions propagate via orphan cleanup
- Set `DOCUMENT_SOURCE_ATTR="github"` so cognee routes pages through its document/cognify pipeline

## Setup

1. **Create a GitHub Personal Access Token**  
   Go to **GitHub Settings → Developer settings → Personal access tokens → Tokens (classic)**  
   Generate new token with **repo** scope (read-only is fine for public repos; private repos need repo scope).

2. **Install the connector**  

   ```bash
   # From the repository root:
   pip install -e ./packages/connector/github
   # or with uv:
   uv pip install -e ./packages/connector/github
   ```

3. **Export your token and LLM key**  

   ```bash
   export GITHUB_TOKEN="ghp_..."
   export LLM_API_KEY="sk-..."  # your OpenAI/Anthropic/etc key
   ```

   To read Dependabot alerts, the token also needs alert read access. Fine-grained tokens need
   **Dependabot alerts: Read-only**. Classic tokens need the `security_events` scope; for public
   repositories, `public_repo` is sufficient. Dependabot alert ingestion is read-only.

4. **Run the example**  

   ```bash
   uv run python packages/connector/github/examples/example.py
   ```

## Example Usage

```python
import cognee
from cognee_community_connector_github import github_source

# Ingest specific repos (omit both to get your authenticated user's repos)
source = github_source(
    repos=["owner/repo1"],
    include_dependabot_alerts=True,
    # Dismissal comments are excluded unless explicitly requested:
    # include_dismissal_comments=True,
)

await cognee.remember(source, dataset_name="github")

# Now search
answer = await cognee.search(
    query_text="Summarize what these GitHub repos are about.",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["github"],
)
print(answer)
```

Dependabot alerts are opt-in and require an explicit `repos=[...]` list. The connector fetches
open, fixed, dismissed, and auto-dismissed alerts, following GitHub's pagination links. Alert
documents use the repository ID and alert number for identity, so state changes update the same
document. Dismissal reasons and recorded dates are included; dismissal comments are omitted by
default. Re-running with unchanged alert data produces the same document content.

If an alert endpoint is unavailable, access is missing, or any page fails, the sync raises and
the previous dlt snapshot is retained. It does not interpret a failed read as an empty alert list.

## Forget-on-delete & incremental behaviour

Each run is a **full snapshot**: the connector replaces the staging table with exactly what is visible via the API.

- If a repo, issue, PR, commit, or release vanishes from GitHub (deleted, made private, or you lose access), it is absent from the snapshot and Cognee's `orphan_cleanup` removes it from memory on the next sync.
- If the title/body changes, the row's `id` stays the same (stable ID) so the row updates in-place and Cognee does **not** re-cognify unchanged content.

## Notes

- Commits only include the commit message and list of changed filenames (never full diffs for large PRs — metadata plus summaries only).
- Issue/PR `content` includes the body **plus** all comments.
- Release `content` includes the release name and notes.
- Token is read from `GITHUB_TOKEN` environment variable if not passed explicitly.
