"""Runs examples/example.py end to end with Gramps Web faked and the LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_gramps import small_tree
from helpers import client_for

from cognee_community_connector_gramps import gramps_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("gramps_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_backfills_recalls_and_resyncs(mocked_llm, monkeypatch, capsys):
    client = client_for(small_tree())
    example = _load_example()
    monkeypatch.setenv("GRAMPS_URL", "https://gramps.test")
    monkeypatch.setenv("GRAMPS_USERNAME", "owner")
    monkeypatch.setenv("GRAMPS_PASSWORD", "example-password")
    monkeypatch.setattr(
        example,
        "gramps_source",
        lambda resource_name: gramps_source(client=client, resource_name=resource_name),
    )

    await example.main()

    output = capsys.readouterr().out.splitlines()
    stats = [line for line in output if line.startswith("sync stats:")]
    assert len(stats) == 2
    assert "'synced': 4" in stats[0]
    assert "'full': 0" in stats[1]  # the second run finds nothing changed
    assert any(line.startswith("Family tree answer:") for line in output)


@pytest.mark.asyncio
async def test_the_example_explains_missing_settings_instead_of_crashing(monkeypatch, capsys):
    for name in ("GRAMPS_URL", "GRAMPS_USERNAME", "GRAMPS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    await _load_example().main()

    assert "Set GRAMPS_URL, GRAMPS_USERNAME, GRAMPS_PASSWORD" in capsys.readouterr().out
