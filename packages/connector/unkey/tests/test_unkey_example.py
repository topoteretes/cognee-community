"""Runs examples/example.py end to end with Unkey faked and LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_unkey import FakeUnkey

from cognee_community_connector_unkey import unkey_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("unkey_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_example_runs_end_to_end(mocked_llm, monkeypatch, capsys):
    fake = FakeUnkey()
    fake.add_api("api_1", "ProductionGateway")
    fake.add_key("api_1", "key_1", "CustKey")
    fake.add_identity("id_1", externalId="cust_acme")

    example = _load_example()
    monkeypatch.setenv("UNKEY_ROOT_KEY", "unkey_test_token_123")
    monkeypatch.setattr(
        example,
        "unkey_source",
        lambda **kwargs: unkey_source(
            service=fake,
            resource_name=kwargs.get("resource_name", "unkey_main_workspace"),
            include=kwargs.get("include", ("apis", "keys", "identities")),
        ),
    )

    await example.main()

    out = capsys.readouterr().out
    assert "=== Unkey sync #1" in out
    assert "=== Unkey sync #2" in out
    assert "Unkey answer:" in out


@pytest.mark.asyncio
async def test_example_handles_missing_token(monkeypatch, capsys):
    monkeypatch.delenv("UNKEY_ROOT_KEY", raising=False)
    example = _load_example()
    await example.main()
    out = capsys.readouterr().out
    assert "Set UNKEY_ROOT_KEY" in out
