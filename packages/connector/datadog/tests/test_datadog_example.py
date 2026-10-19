"""Runs examples/example.py end to end with Datadog faked and the LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_datadog import FakeDatadog, client_for

from cognee_community_connector_datadog import datadog_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("datadog_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_backfills_searches_and_resyncs(mocked_llm, monkeypatch, capsys):
    fake = FakeDatadog()
    fake.add_monitor(1, "Checkout latency", "Runbook: page Alphaperson.")
    fake.add_notebook(10, "Postmortem: checkout outage", "Bravoperson rolled back.")

    example = _load_example()
    monkeypatch.setenv("DD_API_KEY", "example-api-key")
    monkeypatch.setenv("DD_APP_KEY", "example-app-key")
    monkeypatch.setattr(
        example,
        "datadog_source",
        lambda api_key, app_key, site, resource_name: datadog_source(
            service=client_for(fake, site=site), resource_name=resource_name
        ),
    )

    await example.main()

    output = capsys.readouterr().out.splitlines()
    stats = [line for line in output if line.startswith("sync stats:")]
    assert len(stats) == 2
    assert "'failed': 0" in stats[0]
    assert "'skipped': 2" in stats[1]  # the second run finds nothing new
    assert any(line.startswith("On-call answer:") for line in output)


@pytest.mark.asyncio
async def test_the_example_explains_missing_keys_instead_of_crashing(monkeypatch, capsys):
    monkeypatch.delenv("DD_API_KEY", raising=False)
    monkeypatch.delenv("DD_APP_KEY", raising=False)

    await _load_example().main()

    assert "Set DD_API_KEY and DD_APP_KEY" in capsys.readouterr().out
