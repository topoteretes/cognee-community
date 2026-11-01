"""Runs examples/example.py end to end with Sentry faked and the LLM mocked.

The example is what a reader copies, so it is worth proving it still runs: the merged
Apollo and OneNote connectors both ship a test like this.
"""

import importlib.util
import pathlib

import pytest
from fake_sentry import FakeSentry, client_for

from cognee_community_connector_sentry import sentry_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("sentry_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_syncs_searches_and_reports_its_stats(mocked_llm, monkeypatch, capsys):
    fake = FakeSentry()
    fake.add_issue("i1", title="ValueError: Alphacorp checkout failed")
    example = _load_example()
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", fake.valid_token)
    monkeypatch.setenv("SENTRY_ORG", fake.org)
    monkeypatch.setattr(
        example,
        "sentry_source",
        lambda **kwargs: sentry_source(
            service=client_for(fake), org_slug=fake.org, event_sample=kwargs.get("event_sample", 1)
        ),
    )

    await example.main()

    output = capsys.readouterr().out
    assert "Sync stats:" in output
    assert "'completed': True" in output, output
    assert "did not finish" not in output


@pytest.mark.asyncio
async def test_the_example_says_nothing_and_exits_without_credentials(monkeypatch, capsys):
    example = _load_example()
    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("SENTRY_ORG", raising=False)

    await example.main()

    assert "Set SENTRY_AUTH_TOKEN and SENTRY_ORG" in capsys.readouterr().out
