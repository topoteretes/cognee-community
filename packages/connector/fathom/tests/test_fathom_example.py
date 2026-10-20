"""The shipped example must stay runnable: importable, and honest when there is no key."""

import importlib.util
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "example.py"


def _load():
    spec = importlib.util.spec_from_file_location("fathom_example", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_example_asks_for_the_merge_disposition_the_source_requires():
    assert _load().FATHOM_REMEMBER_KWARGS == {"primary_key": "id", "write_disposition": "merge"}


@pytest.mark.asyncio
async def test_without_a_key_the_example_explains_instead_of_crashing(monkeypatch, capsys):
    monkeypatch.delenv("FATHOM_API_KEY", raising=False)

    await _load().main()

    assert "FATHOM_API_KEY" in capsys.readouterr().out
