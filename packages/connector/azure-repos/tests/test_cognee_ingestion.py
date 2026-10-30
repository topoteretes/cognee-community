"""End-to-end through the real ``cognee.add()`` pipeline, Azure DevOps faked.

No cognify, so no LLM. Checks the Data record layer: PRs land as documents
tagged with the connector's source, an unchanged PR keeps its data_id, a PR
with a new comment gets a new one, and PRs of a deleted repo are forgotten.
"""

import cognee
import pytest
import pytest_asyncio
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from cognee.tasks.ingestion.dlt_utils import is_dlt_sourced
from test_azure_repos import PROJECT, FakeAzureRepos, pull_request, repo, text_comment

from cognee_community_connector_azure_repos import azure_repos_source

DATASET = "azure_repos_test"


@pytest_asyncio.fixture
async def clean_cognee(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


async def _documents():
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="write", datasets=[DATASET]
    )
    if not datasets:
        return {}
    return {
        d.system_metadata["external_id"]: d
        for d in await get_dataset_data(datasets[0].id)
        if isinstance(d.system_metadata, dict) and d.system_metadata.get("source") == "azure_repos"
    }


async def _sync(fake):
    await cognee.add(
        azure_repos_source(project=PROJECT, client=fake),
        dataset_name=DATASET,
        write_disposition="merge",
    )


@pytest.mark.asyncio
async def test_sync_comment_and_repo_deletion_through_cognee(clean_cognee):
    fake = FakeAzureRepos([repo("r1", "shop-api"), repo("r2", "shop-web")])
    fake.add("r1", pull_request(1, "Retry card payments"), threads=[])
    fake.add("r1", pull_request(2, "Untouched"))
    fake.add("r2", pull_request(3, "Old web change"))

    await _sync(fake)
    first = await _documents()
    assert set(first) == {"r1/1", "r1/2", "r2/3"}
    assert not any(is_dlt_sourced(d) for d in first.values())
    assert first["r1/1"].system_metadata["title"] == "Pull request !1: Retry card payments"

    fake.threads[("r1", 1)].append(
        {"id": 7, "lastUpdatedDate": "2026-10-18T10:00:00Z", "comments": [text_comment("LGTM")]}
    )
    fake.repos = [repo("r1", "shop-api")]
    await _sync(fake)

    second = await _documents()
    assert set(second) == {"r1/1", "r1/2"}
    assert second["r1/1"].id != first["r1/1"].id
    assert second["r1/2"].id == first["r1/2"].id
