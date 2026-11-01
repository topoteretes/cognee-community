"""Runs examples/example.py end to end with Salesforce faked and the LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_salesforce import FakeSalesforce

from cognee_community_connector_salesforce import salesforce as module

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("salesforce_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_syncs_searches_and_reports_its_stats(mocked_llm, monkeypatch, capsys):
    fake = FakeSalesforce()
    fake.add("Account", Name="Alphacorp Logistics")
    example = _load_example()
    monkeypatch.setenv("SALESFORCE_LOGIN_URL", fake.login)
    monkeypatch.setenv("SALESFORCE_CLIENT_ID", "example-id")
    monkeypatch.setenv("SALESFORCE_CLIENT_SECRET", "example-secret")
    built: list[tuple] = []
    real_client = module.SalesforceClient

    def build(login, client_id, client_secret, **kwargs):
        # the example's arguments reach the client; the fake stands in for Salesforce
        built.append((login, client_id, client_secret))
        return real_client(login, fake.client_id, fake.client_secret, http=fake.client())

    monkeypatch.setattr(module, "SalesforceClient", build)

    await example.main()

    assert built == [(fake.login, "example-id", "example-secret")]

    output = capsys.readouterr().out
    assert "Sync stats:" in output
    assert "'completed': True" in output, output
    assert "did not finish" not in output


@pytest.mark.asyncio
async def test_the_example_says_so_and_exits_without_credentials(monkeypatch, capsys):
    example = _load_example()
    for name in ("SALESFORCE_LOGIN_URL", "SALESFORCE_CLIENT_ID", "SALESFORCE_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)

    await example.main()

    assert "Set SALESFORCE_LOGIN_URL" in capsys.readouterr().out
