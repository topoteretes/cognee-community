"""End-to-end through ``cognee.add`` (no LLM needed): a record deleted in Zoho
leaves the cognee dataset on the next sync. The ``cognify`` graph check runs only
when ``LLM_API_KEY`` is set."""

import asyncio
import os

import cognee
import pytest

from tests.test_zoho_crm import FakeZoho, _deal, _lead, _src

DLT = {"write_disposition": "merge", "primary_key": "id"}


@pytest.fixture
def isolated_cognee(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "false")
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.data_root_directory(str(tmp_path / "data"))


async def _titles(name):
    dataset = next(d for d in await cognee.datasets.list_datasets() if d.name == name)
    items = await cognee.datasets.list_data(dataset.id)
    return sorted((i.system_metadata or {}).get("title") for i in items)


def test_deleted_record_leaves_the_dataset(isolated_cognee):
    fake = FakeZoho(
        {"Leads": [_lead("1", name="Ann Lee"), _lead("2", name="Bo Kim")], "Deals": [_deal()]}
    )

    async def scenario():
        kw = {"dataset_name": "zoho_e2e", "dlt_config": DLT, "skip_connection_test": True}
        await cognee.add(_src(fake, resource_name="zoho_e2e", include_notes=False), **kw)
        first = await _titles("zoho_e2e")
        fake.records["Leads"] = [fake.records["Leads"][0]]
        fake.deleted["Leads"] = [{"id": "2", "deleted_time": "2999-01-01T00:00:00+00:00"}]
        await cognee.add(_src(fake, resource_name="zoho_e2e", include_notes=False), **kw)
        return first, await _titles("zoho_e2e")

    first, second = asyncio.run(scenario())
    assert first == ["Ann Lee", "Big deal", "Bo Kim"]
    assert second == ["Ann Lee", "Big deal"]


@pytest.mark.skipif(not os.environ.get("LLM_API_KEY"), reason="cognify needs an LLM")
def test_deleted_record_leaves_the_graph(isolated_cognee):  # pragma: no cover - needs LLM
    fake = FakeZoho({"Leads": [_lead("1", name="Zebra-lead")], "Deals": []})

    async def graph_text():
        from cognee.infrastructure.databases.graph import get_graph_engine

        nodes, _ = await (await get_graph_engine()).get_graph_data()
        return " ".join(str(props) for _, props in nodes)

    async def scenario():
        await cognee.add(_src(fake, resource_name="zoho_g"), dataset_name="zoho_g", dlt_config=DLT)
        await cognee.cognify(datasets=["zoho_g"])
        before = await graph_text()
        fake.records["Leads"] = []
        fake.deleted["Leads"] = [{"id": "1", "deleted_time": "2999-01-01T00:00:00+00:00"}]
        await cognee.add(_src(fake, resource_name="zoho_g"), dataset_name="zoho_g", dlt_config=DLT)
        return before, await graph_text()

    before, after = asyncio.run(scenario())
    assert "Zebra-lead" in before and "Zebra-lead" not in after
