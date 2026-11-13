"""Runs examples/example.py end to end with Copper faked and the LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_copper import FakeCopper

from cognee_community_connector_copper import copper_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("copper_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_backfills_searches_and_resyncs(mocked_llm, monkeypatch, capsys):
    fake = FakeCopper()
    fake.add_company(1, "Acme Corp")
    fake.add_opportunity(3, "Acme renewal", company_id=1, company_name="Acme Corp")
    fake.add_activity(5, "Alphaperson worries about SSO.", ("opportunity", 3))

    example = _load_example()
    monkeypatch.setenv("COPPER_API_KEY", "example-key")
    monkeypatch.setenv("COPPER_USER_EMAIL", "owner@acme.example")
    monkeypatch.setattr(
        example,
        "copper_source",
        lambda api_key, user_email, resource_name: copper_source(
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
@pytest.mark.parametrize("missing", ["COPPER_API_KEY", "COPPER_USER_EMAIL"])
async def test_the_example_explains_missing_credentials_instead_of_crashing(
    monkeypatch, capsys, missing
):
    monkeypatch.setenv("COPPER_API_KEY", "example-key")
    monkeypatch.setenv("COPPER_USER_EMAIL", "owner@acme.example")
    monkeypatch.delenv(missing)

    await _load_example().main()

    assert "Set COPPER_API_KEY and COPPER_USER_EMAIL" in capsys.readouterr().out
