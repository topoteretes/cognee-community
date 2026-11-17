"""End-to-end through the real ``cognee.add()`` pipeline, Azure DevOps faked.

No cognify, so no LLM is needed. Checks what happens at the Data record layer:
work items land as documents tagged with the connector's source (so cognify
would run entity extraction on them), an unchanged work item keeps its data_id
across syncs, a changed one gets a new one, and work items deleted or destroyed
upstream are forgotten by cognee's orphan cleanup.
"""

import cognee
import pytest
import pytest_asyncio
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from cognee.tasks.ingestion.dlt_utils import is_dlt_sourced
from test_azure_devops_boards import PROJECT, FakeAzureDevOps, work_item

from cognee_community_connector_azure_devops_boards import azure_devops_boards_source

DATASET = "azure_devops_boards_test"


@pytest_asyncio.fixture
async def clean_cognee(tmp_path, monkeypatch):
    # add() never calls the LLM, but cognee's startup check would try to reach one.
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
        if isinstance(d.system_metadata, dict)
        and d.system_metadata.get("source") == "azure_devops_boards"
    }


async def _sync(fake):
    await cognee.add(
        azure_devops_boards_source(project=PROJECT, client=fake),
        dataset_name=DATASET,
        write_disposition="merge",
    )


@pytest.mark.asyncio
async def test_sync_edit_delete_and_destroy_through_cognee(clean_cognee):
    fake = FakeAzureDevOps(
        [
            work_item(1, "Card payments fail on Safari"),
            work_item(2, "Add dark mode"),
            work_item(3, "Old spike"),
            work_item(4, "Untouched story"),
        ]
    )

    await _sync(fake)
    first = await _documents()
    assert set(first) == {"1", "2", "3", "4"}
    # Tagged with the connector's source, not "dlt", so cognify treats each
    # one as a text document and extracts entities from it.
    assert not any(is_dlt_sourced(d) for d in first.values())
    assert first["1"].system_metadata["title"] == "Bug #1: Card payments fail on Safari"

    fake.add_comment(1, "Only happens when 3DS is triggered.")
    fake.delete(2)
    fake.destroy(3)
    await _sync(fake)

    second = await _documents()
    assert set(second) == {"1", "4"}
    assert second["1"].id != first["1"].id  # new content, new data_id
    assert second["4"].id == first["4"].id  # unchanged, not re-ingested
