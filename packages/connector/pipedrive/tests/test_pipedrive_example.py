"""Runs examples/example.py end to end with Pipedrive faked and the LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_pipedrive import FakePipedrive

from cognee_community_connector_pipedrive import pipedrive_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("pipedrive_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_backfills_searches_and_resyncs(mocked_llm, monkeypatch, capsys):
    fake = FakePipedrive()
    fake.add_organization(1, "Acme Corp")
    fake.add_deal(3, "Acme renewal", org_id=1)
    fake.add_note(5, "<p>Alphaperson worries about SSO.</p>", deal_id=3)

    example = _load_example()
    monkeypatch.setenv("PIPEDRIVE_API_TOKEN", "example-token")
    monkeypatch.setattr(
        example,
        "pipedrive_source",
        lambda api_token, resource_name: pipedrive_source(
            service=fake, resource_name=resource_name
        ),
    )

    await example.main()

    output = capsys.readouterr().out.splitlines()
    stats = [line for line in output if line.startswith("sync stats:")]
    assert len(stats) == 2
    assert "'failed': 0" in stats[0]
    assert "'skipped': 3" in stats[1]  # the second run finds nothing new
    assert any(line.startswith("CRM answer:") for line in output)


@pytest.mark.asyncio
async def test_the_example_explains_a_missing_token_instead_of_crashing(monkeypatch, capsys):
    monkeypatch.delenv("PIPEDRIVE_API_TOKEN", raising=False)

    await _load_example().main()

    assert "Set PIPEDRIVE_API_TOKEN" in capsys.readouterr().out
