"""Runs examples/example.py end to end with Sanity faked and the LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_sanity import FakeSanity, block

from cognee_community_connector_sanity import sanity_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("sanity_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_syncs_answers_and_resyncs(mocked_llm, monkeypatch, capsys):
    fake = FakeSanity()
    fake.put("a1", "article", title="Billing", body=[block("Alphaword owns billing.")])
    example = _load_example()
    monkeypatch.setenv("SANITY_PROJECT_ID", "proj1")
    monkeypatch.setenv("SANITY_DOCUMENT_TYPES", "article")
    monkeypatch.setattr(
        example, "sanity_source", lambda **kwargs: sanity_source(**kwargs, client=fake)
    )

    await example.main()
    await example.main()

    output = capsys.readouterr().out.splitlines()
    stats = [line for line in output if line.startswith("sync stats:")]
    assert len(stats) == 2
    assert "'changed': 1" in stats[0]
    assert "'changed': 0" in stats[1], "the second run finds nothing new"
    assert any(line.startswith("answer:") for line in output)


@pytest.mark.asyncio
async def test_the_example_explains_missing_settings_instead_of_crashing(monkeypatch, capsys):
    monkeypatch.delenv("SANITY_PROJECT_ID", raising=False)
    await _load_example().main()
    assert "Set SANITY_PROJECT_ID" in capsys.readouterr().out
