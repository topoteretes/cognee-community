"""Runs examples/example.py end to end with Attio faked and the LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_attio import sample_workspace

from cognee_community_connector_attio import attio_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("attio_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_syncs_and_searches(mocked_llm, monkeypatch, capsys):
    example = _load_example()
    monkeypatch.setenv("ATTIO_API_KEY", "example-key")
    monkeypatch.setattr(example, "attio_source", lambda: attio_source(client=sample_workspace()))

    await example.main()

    output = capsys.readouterr().out
    assert "Syncing Attio into cognee" in output
    assert "Search result:" in output


@pytest.mark.asyncio
async def test_the_example_explains_a_missing_token_instead_of_crashing(monkeypatch, capsys):
    monkeypatch.delenv("ATTIO_API_KEY", raising=False)

    await _load_example().main()

    assert "Set ATTIO_API_KEY" in capsys.readouterr().out
