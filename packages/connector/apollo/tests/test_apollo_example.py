"""Runs examples/example.py end to end with Apollo faked and the LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_apollo import FakeApollo

from cognee_community_connector_apollo import apollo_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("apollo_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_backfills_searches_and_resyncs(mocked_llm, monkeypatch, capsys):
    fake = FakeApollo()
    fake.add_sequence("s1", "Q4 Outreach")
    fake.add_account("a1", "Acme")
    fake.add_contact("c1", "Ada Alphaperson", account_id="a1")
    fake.enroll("c1", "s1")

    example = _load_example()
    monkeypatch.setenv("APOLLO_API_KEY", "example-key")
    monkeypatch.setattr(
        example, "apollo_source", lambda api_key: apollo_source(service=fake, resource_name="ex")
    )

    await example.main()

    output = capsys.readouterr().out.splitlines()
    stats = [line for line in output if line.startswith("sync stats:")]
    assert len(stats) == 2
    assert "'failed': 0" in stats[0]
    assert "'skipped': 3" in stats[1]  # the second run finds nothing new
    assert any(line.startswith("CRM answer:") for line in output)


@pytest.mark.asyncio
async def test_the_example_explains_a_missing_key_instead_of_crashing(monkeypatch, capsys):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)

    await _load_example().main()

    assert "Set APOLLO_API_KEY" in capsys.readouterr().out
