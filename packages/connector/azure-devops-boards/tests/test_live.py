"""Read-only checks against a real Azure DevOps project.

Skipped unless AZURE_DEVOPS_PAT, AZURE_DEVOPS_ORG and AZURE_DEVOPS_PROJECT are
set. Nothing is written to Azure DevOps. The project needs at least one work item.
"""

import os

import pytest

from cognee_community_connector_azure_devops_boards.azure_devops_boards import (
    AzureDevOpsClient,
    _Scope,
    sync_work_items,
)

PAT = os.environ.get("AZURE_DEVOPS_PAT")
ORG = os.environ.get("AZURE_DEVOPS_ORG")
PROJECT = os.environ.get("AZURE_DEVOPS_PROJECT")

pytestmark = pytest.mark.skipif(
    not (PAT and ORG and PROJECT),
    reason="set AZURE_DEVOPS_PAT, AZURE_DEVOPS_ORG and AZURE_DEVOPS_PROJECT to run",
)


def _client():
    return AzureDevOpsClient(f"https://dev.azure.com/{ORG}", PAT)


def test_full_sync_then_no_op_resync():
    client = _client()
    state: dict = {}

    rows = list(sync_work_items(client, _Scope(project=PROJECT), state))

    live = [r for r in rows if not r["_deleted"]]
    assert live, "the project needs at least one work item"
    for row in live:
        assert row["title"] and row["content"]
        assert row["url"].startswith(f"https://dev.azure.com/{ORG}/")
    assert state["continuation_token"]
    assert len(state["known_ids"]) == len(live)

    # Nothing changed in between, so the second run sends nothing.
    assert list(sync_work_items(client, _Scope(project=PROJECT), state)) == []
