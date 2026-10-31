"""Runs examples/example.py end to end with Svix faked and LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_svix import FakeSvix

from cognee_community_connector_svix import svix_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("svix_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_example_runs_end_to_end(mocked_llm, monkeypatch, capsys):
    fake = FakeSvix()
    fake.add_app("app_1", "ProductionApp")
    fake.add_endpoint("app_1", "ep_1", "https://api.example.com/hook")
    fake.add_event_type("user.created")

    example = _load_example()
    monkeypatch.setenv("SVIX_API_TOKEN", "test_svix_token_123")
    monkeypatch.setattr(
        example,
        "svix_source",
        lambda **kwargs: svix_source(
            service=fake,
            resource_name=kwargs.get("resource_name", "svix_main_workspace"),
            include=kwargs.get("include", ("applications", "endpoints", "event_types")),
        ),
    )

    await example.main()

    out = capsys.readouterr().out
    assert "=== Svix sync #1" in out
    assert "=== Svix sync #2" in out
    assert "Webhook answer:" in out


@pytest.mark.asyncio
async def test_example_handles_missing_token(monkeypatch, capsys):
    monkeypatch.delenv("SVIX_API_TOKEN", raising=False)
    example = _load_example()
    await example.main()
    out = capsys.readouterr().out
    assert "Set SVIX_API_TOKEN" in out
