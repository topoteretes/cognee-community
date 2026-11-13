"""Runs examples/example.py end to end with Trigger.dev faked and the LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_triggerdev import KEY, FakeTriggerDev

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("triggerdev_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_backfills_searches_and_resyncs(mocked_llm, monkeypatch, capsys):
    fake = FakeTriggerDev()
    fake.add_schedule("sched_1", "nightly-backup")
    fake.add_error("error_1", "send-invoice")
    fake.add_run("run_1", "send-invoice")
    fake.install(monkeypatch)
    monkeypatch.setenv("TRIGGER_SECRET_KEY", KEY)

    await _load_example().main()

    output = capsys.readouterr().out.splitlines()
    stats = [line for line in output if line.startswith("sync stats:")]
    assert len(stats) == 2
    assert "'failed': 0" in stats[0]
    assert "'skipped': 3" in stats[1]  # the second run finds nothing new
    assert any(line.startswith("Ops answer:") for line in output)


@pytest.mark.asyncio
async def test_the_example_explains_a_missing_key_instead_of_crashing(monkeypatch, capsys):
    monkeypatch.delenv("TRIGGER_SECRET_KEY", raising=False)

    await _load_example().main()

    assert "Set TRIGGER_SECRET_KEY" in capsys.readouterr().out
