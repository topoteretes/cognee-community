"""Read-only checks against a real Azure DevOps project.

Skipped unless AZURE_DEVOPS_PAT (with Code Read), AZURE_DEVOPS_ORG and
AZURE_DEVOPS_PROJECT are set. Nothing is written. The project needs at least
one repository with a pull request.
"""

import os

import pytest

from cognee_community_connector_azure_repos.azure_repos import (
    AzureDevOpsClient,
    _Scope,
    sync_pull_requests,
)

PAT = os.environ.get("AZURE_DEVOPS_PAT")
ORG = os.environ.get("AZURE_DEVOPS_ORG")
PROJECT = os.environ.get("AZURE_DEVOPS_PROJECT")

pytestmark = pytest.mark.skipif(
    not (PAT and ORG and PROJECT),
    reason="set AZURE_DEVOPS_PAT, AZURE_DEVOPS_ORG and AZURE_DEVOPS_PROJECT to run",
)


def test_full_sync_then_no_op_resync():
    client = AzureDevOpsClient(f"https://dev.azure.com/{ORG}", PAT)
    state: dict = {}

    rows = list(sync_pull_requests(client, _Scope(project=PROJECT), state))

    live = [r for r in rows if not r["_deleted"]]
    assert live, "the project needs at least one pull request"
    for row in live:
        assert row["title"].startswith("Pull request !") and row["content"]
        assert "/pullrequest/" in row["url"]

    assert list(sync_pull_requests(client, _Scope(project=PROJECT), state)) == []
