"""Runs examples/example.py end to end with Ramp faked and the LLM mocked.

The example is what a reader copies, so it is worth proving it still runs: the merged
Apollo and OneNote connectors both ship a test like this.
"""

import importlib.util
import pathlib

import pytest
from fake_ramp import FakeRamp, ago, client_for

from cognee_community_connector_ramp import ramp_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("ramp_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_syncs_searches_and_reports_its_stats(mocked_llm, monkeypatch, capsys):
    fake = FakeRamp()
    fake.add("t1", ago(1), memo="Alphacorp dinner after the launch")
    example = _load_example()
    monkeypatch.setenv("RAMP_CLIENT_ID", "example-id")
    monkeypatch.setenv("RAMP_CLIENT_SECRET", "example-secret")
    monkeypatch.setattr(
        example,
        "ramp_source",
        lambda **kwargs: ramp_source(
            resource_name=kwargs.get("resource_name", "ramp_example"),
            service=client_for(fake),
        ),
    )

    await example.main()

    output = capsys.readouterr().out
    assert "Sync stats:" in output
    assert "'scanned': 1" in output, output


@pytest.mark.asyncio
async def test_the_example_says_so_and_exits_without_credentials(monkeypatch, capsys):
    example = _load_example()
    monkeypatch.delenv("RAMP_CLIENT_ID", raising=False)
    monkeypatch.delenv("RAMP_CLIENT_SECRET", raising=False)

    await example.main()

    assert "Set RAMP_CLIENT_ID and RAMP_CLIENT_SECRET" in capsys.readouterr().out
