"""Runs examples/example.py end to end with Rollbar faked and the LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_rollbar import PROJECT, TOKEN, FakeRollbar

from cognee_community_connector_rollbar import rollbar_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("rollbar_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_backfills_searches_and_resyncs(mocked_llm, monkeypatch, capsys):
    fake = FakeRollbar()
    fake.add_item("ValueError: Alphacoupon code rejected")
    fake.add_item("KeyError: 'plan_tier'", environment="staging")

    example = _load_example()
    monkeypatch.setenv("ROLLBAR_PROJECT_ID", str(PROJECT))
    monkeypatch.setenv("ROLLBAR_ACCOUNT_SLUG", "acme")
    calls = []

    def fake_source(project_id, *, account_slug):
        calls.append((project_id, account_slug))
        return rollbar_source(
            project_id,
            access_token=TOKEN,
            account_slug=account_slug,
            http_client=fake.client(),
            resource_name="ex",
        )

    monkeypatch.setattr(example, "rollbar_source", fake_source)

    await example.main()

    output = capsys.readouterr().out.splitlines()
    stats = [line for line in output if line.startswith("sync stats:")]
    assert len(stats) == 2
    assert "'failed': 0" in stats[0]
    assert "'skipped': 2" in stats[1]  # the second run finds nothing new
    assert any(line.startswith("Errors answer:") for line in output)
    assert calls == [(str(PROJECT), "acme")] * 2


@pytest.mark.asyncio
async def test_the_example_explains_a_missing_project_instead_of_crashing(monkeypatch, capsys):
    monkeypatch.delenv("ROLLBAR_PROJECT_ID", raising=False)

    await _load_example().main()

    assert "Set ROLLBAR_PROJECT_ID" in capsys.readouterr().out
