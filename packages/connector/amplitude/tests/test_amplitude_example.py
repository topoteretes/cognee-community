"""Runs examples/example.py end to end with Amplitude faked and the LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_amplitude import FakeAmplitude

from cognee_community_connector_amplitude import amplitude_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("amplitude_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_backfills_searches_and_resyncs(mocked_llm, monkeypatch, capsys):
    fake = FakeAmplitude()
    fake.add_event("Checkout Completed", description="A customer paid for an order.")
    fake.add_event_property("Checkout Completed", "cart_value", type="number")
    fake.add_cohort("c1", "Alphacohort paying users")
    fake.add_annotation(1, "Checkout redesign shipped")
    fake.add_chart("ch1", "Checkouts per day", "Uniques", "Checkout Completed")

    example = _load_example()
    monkeypatch.setenv("AMPLITUDE_API_KEY", "example-key")
    monkeypatch.setenv("AMPLITUDE_SECRET_KEY", "example-secret")
    monkeypatch.delenv("AMPLITUDE_REGION", raising=False)
    monkeypatch.setenv("AMPLITUDE_CHART_IDS", "ch1")
    regions = []

    def fake_source(api_key, secret_key, region, chart_ids):
        regions.append(region)
        return amplitude_source(service=fake, resource_name="ex", chart_ids=chart_ids)

    monkeypatch.setattr(example, "amplitude_source", fake_source)

    await example.main()

    output = capsys.readouterr().out.splitlines()
    stats = [line for line in output if line.startswith("sync stats:")]
    assert len(stats) == 2
    assert "'failed': 0" in stats[0]
    assert "'skipped': 4" in stats[1]  # the second run finds nothing new
    assert any(line.startswith("Analytics answer:") for line in output)
    assert regions == ["us", "us"]


@pytest.mark.parametrize("missing", ["AMPLITUDE_API_KEY", "AMPLITUDE_SECRET_KEY"])
@pytest.mark.asyncio
async def test_the_example_explains_a_missing_key_instead_of_crashing(monkeypatch, capsys, missing):
    monkeypatch.setenv("AMPLITUDE_API_KEY", "example-key")
    monkeypatch.setenv("AMPLITUDE_SECRET_KEY", "example-secret")
    monkeypatch.delenv(missing)

    await _load_example().main()

    assert "Set AMPLITUDE_API_KEY and AMPLITUDE_SECRET_KEY" in capsys.readouterr().out
