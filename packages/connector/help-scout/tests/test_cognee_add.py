"""End-to-end through ``cognee.add`` (no LLM needed): a conversation deleted in
Help Scout leaves the cognee dataset on the next sync.

``cognify`` needs an LLM; the graph step requires ``--run-llm-tests`` and a key.
"""

import asyncio
import os

import cognee
import pytest

from tests.test_help_scout import FakeHelpScout, _conv, _src

DLT = {"write_disposition": "merge", "primary_key": "id"}


@pytest.fixture
def isolated_cognee(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "false")
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.data_root_directory(str(tmp_path / "data"))
    return tmp_path


async def _titles(dataset_name):
    datasets = await cognee.datasets.list_datasets()
    dataset = next(d for d in datasets if d.name == dataset_name)
    items = await cognee.datasets.list_data(dataset.id)
    return sorted((item.system_metadata or {}).get("title") for item in items)


def test_deleted_conversation_leaves_the_dataset(isolated_cognee):
    fake = FakeHelpScout([_conv(1), _conv(2, subject="Invoice"), _conv(3)])

    async def scenario():
        await cognee.add(
            _src(fake, resource_name="hs_e2e"),
            dataset_name="hs_e2e",
            dlt_config=DLT,
            skip_connection_test=True,
        )
        first = await _titles("hs_e2e")
        fake.conversations = [c for c in fake.conversations if c["id"] != 2]
        await cognee.add(
            _src(fake, resource_name="hs_e2e"),
            dataset_name="hs_e2e",
            dlt_config=DLT,
            skip_connection_test=True,
        )
        return first, await _titles("hs_e2e")

    first, second = asyncio.run(scenario())
    assert first == ["#101 Refund", "#102 Invoice", "#103 Refund"]
    assert second == ["#101 Refund", "#103 Refund"]


def test_deleted_conversation_leaves_the_graph(isolated_cognee, request):  # pragma: no cover
    if not request.config.getoption("--run-llm-tests"):
        pytest.skip("live cognify requires explicit --run-llm-tests")
    if not os.environ.get("LLM_API_KEY"):
        pytest.skip("live cognify requires LLM_API_KEY")
    fake = FakeHelpScout([_conv(1, subject="Zebra-ticket")])

    async def graph_text():
        from cognee.infrastructure.databases.graph import get_graph_engine

        nodes, _ = await (await get_graph_engine()).get_graph_data()
        return " ".join(str(props) for _, props in nodes)

    async def scenario():
        await cognee.add(_src(fake, resource_name="hs_g"), dataset_name="hs_g", dlt_config=DLT)
        await cognee.cognify(datasets=["hs_g"])
        before = await graph_text()
        fake.conversations = []
        await cognee.add(_src(fake, resource_name="hs_g"), dataset_name="hs_g", dlt_config=DLT)
        return before, await graph_text()

    before, after = asyncio.run(scenario())
    assert "Zebra-ticket" in before
    assert "Zebra-ticket" not in after
