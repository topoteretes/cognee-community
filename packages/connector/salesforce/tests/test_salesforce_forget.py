"""End-to-end: Salesforce changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify) against the
fake org, with a mocked LLM and mocked embeddings (see conftest.py). Each record names one
company, which the mocked LLM turns into exactly one graph entity.
"""

import cognee
import httpx
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_dataset_data, get_datasets_by_name
from cognee.modules.users.methods import get_default_user
from cognee.tasks.ingestion.exceptions.exceptions import DLTIngestionError
from fake_salesforce import FakeSalesforce, client_for

from cognee_community_connector_salesforce import salesforce_source

DATASET = "salesforce_forget_test"


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


async def _documents() -> set[str]:
    user = await get_default_user()
    dataset = (await get_datasets_by_name([DATASET], user.id))[0]
    return {
        (data.system_metadata or {}).get("external_id")
        for data in await get_dataset_data(dataset.id)
    }


async def _sync(fake: FakeSalesforce) -> None:
    await cognee.remember(
        salesforce_source(service=client_for(fake), resource_name="salesforce_test"),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        self_improvement=False,
    )


@pytest.mark.asyncio
async def test_forgotten_records_leave_the_graph(mocked_llm):
    fake = FakeSalesforce()
    alpha = fake.add("Account", Name="Alphacorp Logistics")
    case = fake.add(
        "Case",
        CaseNumber="00001",
        Subject="Bravocorp reports stale tracking",
        Status="New",
        IsClosed=False,
    )

    await _sync(fake)
    assert await _graph_has("Alphacorp")
    assert await _graph_has("Bravocorp")
    assert await _documents() == {f"account:{alpha}", f"case:{case}"}

    # a failing API must not forget anything
    fake.queued = [httpx.Response(500, content=b"{}")] * 10
    with pytest.raises(DLTIngestionError) as failure:
        await _sync(fake)
    assert "salesforce" in str(failure.value).lower()
    fake.queued = []
    assert await _graph_has("Alphacorp")

    # a comment on the case changes the case's document, though the case itself did not move
    fake.add_comment(case, "Workaround sent.")
    await _sync(fake)
    assert await _documents() == {f"account:{alpha}", f"case:{case}"}

    # the account is deleted in Salesforce: getDeleted reports it
    fake.delete("Account", alpha)
    fake.tick(60)  # getDeleted needs a window of a minute or more
    await _sync(fake)
    assert not await _graph_has("Alphacorp"), "a deleted record must be forgotten"
    assert await _graph_has("Bravocorp"), "an untouched record must stay"
    assert await _documents() == {f"case:{case}"}
