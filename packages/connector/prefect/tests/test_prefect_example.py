"""Runs examples/example.py end to end with Prefect faked and LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_prefect import FakePrefect

from cognee_community_connector_prefect import prefect_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("prefect_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_example_runs_end_to_end(mocked_llm, monkeypatch, capsys):
    fake = FakePrefect()
    fake.add_flow("f1", "ECommerceFlow", tags=["critical"])
    fake.add_deployment("d1", "ETLDeployment", "f1")
    fake.add_flow_run("r1", "Run 1", "f1", deployment_id="d1")

    example = _load_example()
    monkeypatch.setattr(
        example,
        "prefect_source",
        lambda **kwargs: prefect_source(
            service=fake,
            resource_name=kwargs.get("resource_name", "prefect_workspace_main"),
            include=kwargs.get("include", ("flows", "deployments", "flow_runs")),
        ),
    )

    await example.main()

    out = capsys.readouterr().out
    assert "=== Prefect sync #1" in out
    assert "=== Prefect sync #2" in out
    assert "Orchestrator answer:" in out
