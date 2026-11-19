"""Runs examples/example.py end to end with Moodle faked and the LLM mocked."""

import importlib.util
import pathlib

import pytest
from fake_moodle import FakeMoodle

from cognee_community_connector_moodle import moodle_source

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("moodle_example", EXAMPLE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_example_backfills_searches_and_resyncs(mocked_llm, monkeypatch, capsys):
    fake = FakeMoodle()
    fake.add_course(7, "Algorithms", summary="<p>Sorting and searching.</p>")
    fake.add_page(7, "Sorting", "<p>Ada Alphaperson explains merge sort.</p>")
    fake.add_assignment(7, "HW1", "<p>Implement merge sort.</p>")

    example = _load_example()
    monkeypatch.setenv("MOODLE_BASE_URL", "https://moodle.example.edu")
    monkeypatch.setenv("MOODLE_TOKEN", "exampletoken")
    monkeypatch.setattr(
        example,
        "moodle_source",
        lambda resource_name, **kwargs: moodle_source(service=fake, resource_name=resource_name),
    )

    await example.main()

    output = capsys.readouterr().out.splitlines()
    stats = [line for line in output if line.startswith("sync stats:")]
    assert len(stats) == 2
    assert "'failed': 0" in stats[0]
    assert "'skipped': 4" in stats[1]  # course, section, page, assignment: nothing new
    assert any(line.startswith("Course answer:") for line in output)


@pytest.mark.asyncio
async def test_the_example_explains_missing_settings_instead_of_crashing(monkeypatch, capsys):
    monkeypatch.delenv("MOODLE_BASE_URL", raising=False)
    monkeypatch.delenv("MOODLE_TOKEN", raising=False)

    await _load_example().main()

    assert "MOODLE_BASE_URL" in capsys.readouterr().out
