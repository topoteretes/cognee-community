# cognee-community-connector-github-actions

GitHub Actions CI/CD workflows and runs data-source connector for [cognee](https://github.com/topoteretes/cognee).

Ingests CI/CD workflow runs, conclusions, job statuses, and diagnostic step failure points from the GitHub REST API into cognee memory to enable automated flaky test diagnosis and CI failure root cause analysis.

## Usage

```python
import cognee
from cognee_community_connector_github_actions import github_actions_source

# Ingest recent main branch workflow runs
await cognee.remember(
    github_actions_source(
        repository="owner/repo",
        token="ghp_...",  # or GITHUB_TOKEN environment variable
        branch="main",
        limit=25,
    ),
    dataset_name="ci-diagnostics",
)
```

## Features

- **Workflow run tracking**: Ingests run statuses, conclusions (success, failure, cancelled, timed_out), branch, commit SHA, and triggering actors.
- **Diagnostic step extraction**: Inspects jobs to pinpoint the exact failed steps and test commands.
- **Maintainer query ready**: Allows AI agents to answer "which CI tests failed on main this week?" or "why did the release workflow break?".
