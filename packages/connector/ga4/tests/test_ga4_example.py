"""Runs examples/example.py end to end with GA4 faked and the LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_ga4 import PROPERTY, FakeGA4

from cognee_community_connector_ga4 import ga4_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("ga4_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_backfills_searches_and_resyncs(mocked_llm, monkeypatch, capsys):
    fake = FakeGA4()
    fake.add_custom_dimension("11", "plan_tier", "Plan tier", scope="USER")
    fake.add_key_event("31", "purchase")
    fake.add_audience("41", "Alphaaudience purchasers", "Users who bought something.")
    fake.add_annotation("51", "Checkout redesign shipped")

    example = _load_example()
    monkeypatch.setenv("GA4_PROPERTY_ID", PROPERTY)
    property_ids = []

    def fake_source(property_id):
        property_ids.append(property_id)
        return ga4_source(property_id, service=fake, resource_name="ex")

    monkeypatch.setattr(example, "ga4_source", fake_source)

    await example.main()

    output = capsys.readouterr().out.splitlines()
    stats = [line for line in output if line.startswith("sync stats:")]
    assert len(stats) == 2
    assert "'failed': 0" in stats[0]
    assert "'skipped': 4" in stats[1]  # the second run finds nothing new
    assert any(line.startswith("Analytics answer:") for line in output)
    assert property_ids == [PROPERTY, PROPERTY]


@pytest.mark.asyncio
async def test_the_example_explains_a_missing_property_instead_of_crashing(monkeypatch, capsys):
    monkeypatch.delenv("GA4_PROPERTY_ID", raising=False)

    await _load_example().main()

    assert "Set GA4_PROPERTY_ID" in capsys.readouterr().out
