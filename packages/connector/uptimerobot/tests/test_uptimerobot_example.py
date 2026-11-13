"""Runs examples/example.py end to end with UptimeRobot faked and LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_uptimerobot import FakeUptimeRobot

from cognee_community_connector_uptimerobot import uptimerobot_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("uptimerobot_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_example_runs_end_to_end(mocked_llm, monkeypatch, capsys):
    fake = FakeUptimeRobot()
    fake.add_monitor("101", "ProductionMonitor")
    fake.add_psp("201", "UptimeStatusPage", monitors=[101])
    fake.add_alert_contact("301", "SlackAlert")

    example = _load_example()
    monkeypatch.setenv("UPTIMEROBOT_API_KEY", "u123456-test_api_key")
    monkeypatch.setattr(
        example,
        "uptimerobot_source",
        lambda **kwargs: uptimerobot_source(
            service=fake,
            resource_name=kwargs.get("resource_name", "uptimerobot_main_workspace"),
            include=kwargs.get("include", ("monitors", "public_status_pages", "alert_contacts")),
        ),
    )

    await example.main()

    out = capsys.readouterr().out
    assert "=== UptimeRobot sync #1" in out
    assert "=== UptimeRobot sync #2" in out
    assert "Uptime answer:" in out


@pytest.mark.asyncio
async def test_example_handles_missing_key(monkeypatch, capsys):
    monkeypatch.delenv("UPTIMEROBOT_API_KEY", raising=False)
    example = _load_example()
    await example.main()
    out = capsys.readouterr().out
    assert "Set UPTIMEROBOT_API_KEY" in out
