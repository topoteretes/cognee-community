"""End-to-end: Sentry changes must reach cognee's graph, not just dlt rows.

Runs the real cognee pipeline (dlt merge -> document ingestion -> cognify) against the
fake Sentry API, with a mocked LLM and mocked embeddings (see conftest.py). Each issue's
title names one company, which the mocked LLM turns into exactly one graph entity.
"""

import cognee
import httpx
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_dataset_data, get_datasets_by_name
from cognee.modules.users.methods import get_default_user
from cognee.tasks.ingestion.exceptions.exceptions import DLTIngestionError
from fake_sentry import FakeSentry, ago

from cognee_community_connector_sentry import SentryClient, sentry_source

DATASET = "sentry_forget_test"


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _nid, props in nodes for value in (props or {}).values()
    )


async def _documents() -> set[str]:
    """External ids of the documents in the dataset, from the relational store."""
    user = await get_default_user()
    dataset = (await get_datasets_by_name([DATASET], user.id))[0]
    return {
        (data.system_metadata or {}).get("external_id")
        for data in await get_dataset_data(dataset.id)
    }


async def _sync(fake: FakeSentry) -> None:
    client = SentryClient(
        fake.valid_token, org_slug=fake.org, http=fake.client(), sleep=lambda _s: None
    )
    await cognee.remember(
        sentry_source(service=client, org_slug=fake.org, resource_name="sentry_test"),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        self_improvement=False,
    )


@pytest.mark.asyncio
async def test_forgotten_issues_leave_the_graph(mocked_llm):
    fake = FakeSentry()
    fake.add_issue("i1", title="TimeoutError: Alphacorp webhook timed out", last_seen=ago(2))
    fake.add_issue("i2", title="KeyError: Bravocorp account id missing", last_seen=ago(1))

    await _sync(fake)
    assert await _graph_has("Alphacorp")
    assert await _graph_has("Bravocorp")
    assert await _documents() == {"issue:i1", "issue:i2"}

    # a failing API must not forget anything
    fake.queued = [httpx.Response(500, content=b"{}")] * 10
    # cognee wraps dlt's PipelineStepFailed in its own error; naming it is the point,
    # so this cannot pass on an unrelated failure
    with pytest.raises(DLTIngestionError) as failure:
        await _sync(fake)
    assert "sentry" in str(failure.value).lower()
    fake.queued = []
    assert await _graph_has("Alphacorp")

    # resolving an issue keeps it: only a full listing each run can see the new status
    fake.issues["i1"]["status"] = "resolved"
    await _sync(fake)
    assert await _graph_has("Alphacorp"), "a resolved issue must stay in memory"
    assert await _documents() == {"issue:i1", "issue:i2"}

    # the issue is deleted in Sentry: the queued job has run, so it 404s
    fake.delete_issue("i1")
    await _sync(fake)
    assert not await _graph_has("Alphacorp"), "a deleted issue must be forgotten"
    assert await _graph_has("Bravocorp"), "an untouched issue must stay"
    # the document itself is removed (the _deleted hard delete), not left behind empty
    assert await _documents() == {"issue:i2"}
