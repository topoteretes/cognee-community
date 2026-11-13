"""Runs examples/example.py end to end with Checkly faked and LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_checkly import FakeCheckly

from cognee_community_connector_checkly import checkly_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("checkly_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_example_runs_end_to_end(mocked_llm, monkeypatch, capsys):
    fake = FakeCheckly()
    fake.add_check_group("g1", "CheckGroup", tags=["critical"])
    fake.add_check("c1", "APICheck", groupId="g1")
    fake.add_alert_channel("a1", "SlackAlert")

    example = _load_example()
    monkeypatch.setenv("CHECKLY_API_KEY", "cu_test_api_key")
    monkeypatch.setattr(
        example,
        "checkly_source",
        lambda **kwargs: checkly_source(
            service=fake,
            resource_name=kwargs.get("resource_name", "checkly_production_workspace"),
            include=kwargs.get("include", ("checks", "check_groups", "alert_channels")),
        ),
    )

    await example.main()

    out = capsys.readouterr().out
    assert "=== Checkly sync #1" in out
    assert "=== Checkly sync #2" in out
    assert "Monitoring answer:" in out


@pytest.mark.asyncio
async def test_example_handles_missing_key(monkeypatch, capsys):
    monkeypatch.delenv("CHECKLY_API_KEY", raising=False)
    example = _load_example()
    await example.main()
    out = capsys.readouterr().out
    assert "Set CHECKLY_API_KEY" in out
