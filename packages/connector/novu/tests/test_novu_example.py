"""Runs examples/example.py end to end with Novu faked and LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_novu import FakeNovu

from cognee_community_connector_novu import novu_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("novu_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_example_runs_end_to_end(mocked_llm, monkeypatch, capsys):
    fake = FakeNovu()
    fake.add_workflow("wf_1", "SecurityAlertWorkflow")
    fake.add_topic("security-alerts", "NotificationTopic")
    fake.add_integration("integ_1", "sendgrid", "email")

    example = _load_example()
    monkeypatch.setenv("NOVU_API_KEY", "test_novu_api_key_123")
    monkeypatch.setattr(
        example,
        "novu_source",
        lambda **kwargs: novu_source(
            service=fake,
            resource_name=kwargs.get("resource_name", "novu_main_workspace"),
            include=kwargs.get(
                "include",
                ("workflows", "topics", "notification_groups", "integrations"),
            ),
        ),
    )

    await example.main()

    out = capsys.readouterr().out
    assert "=== Novu sync #1" in out
    assert "=== Novu sync #2" in out
    assert "Notification answer:" in out


@pytest.mark.asyncio
async def test_example_handles_missing_key(monkeypatch, capsys):
    monkeypatch.delenv("NOVU_API_KEY", raising=False)
    example = _load_example()
    await example.main()
    out = capsys.readouterr().out
    assert "Set NOVU_API_KEY" in out
