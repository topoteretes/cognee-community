"""Tests for JSR data-source row generation and formatting."""

import pytest
from fake_jsr import FakeJSRClient

from cognee_community_connector_jsr.jsr import (
    _format_package_content,
    _iter_jsr_rows,
    jsr_source,
)


def test_format_package_content():
    content = _format_package_content(
        scope="std",
        name="path",
        latest_version="1.0.8",
        description="Standard path library",
        runtime_compat={"deno": True, "node": True},
        repo_url="https://github.com/denoland/std",
        exports={".": "./mod.ts"},
        readme_text="# Path Guide",
    )
    assert "# JSR Package: @std/path" in content
    assert "**Latest Version:** 1.0.8" in content
    assert "deno, node" in content
    assert "https://github.com/denoland/std" in content
    assert "- `.` -> `./mod.ts`" in content
    assert "# Path Guide" in content


def test_iter_rows_scope_discovery():
    fake = FakeJSRClient()
    rows = list(
        _iter_jsr_rows(
            client=fake,
            scopes=["std"],
            package_tuples=[],
            include_docs=True,
            previous_state={},
        )
    )
    assert len(rows) == 2
    ids = {r["id"] for r in rows}
    assert ids == {"jsr:@std/path", "jsr:@std/fs"}

    path_row = next(r for r in rows if r["id"] == "jsr:@std/path")
    assert path_row["scope"] == "@std"
    assert path_row["name"] == "path"
    assert path_row["latest_version"] == "1.0.8"
    assert path_row["url"] == "https://jsr.io/@std/path"
    assert path_row["_deleted"] is False
    assert "Exports" in path_row["content"]


def test_iter_rows_targeted_package():
    fake = FakeJSRClient()
    rows = list(
        _iter_jsr_rows(
            client=fake,
            scopes=[],
            package_tuples=[("std", "path")],
            include_docs=True,
            previous_state={},
        )
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "jsr:@std/path"


def test_iter_rows_skips_unchanged_on_second_sync():
    fake = FakeJSRClient()
    state = {}
    first_run = list(
        _iter_jsr_rows(
            client=fake,
            scopes=["std"],
            package_tuples=[],
            include_docs=True,
            previous_state=state,
        )
    )
    assert len(first_run) == 2

    # Second run with same state yields zero new rows
    second_run = list(
        _iter_jsr_rows(
            client=fake,
            scopes=["std"],
            package_tuples=[],
            include_docs=True,
            previous_state=state,
        )
    )
    assert len(second_run) == 0


def test_source_rejects_empty_selection():
    with pytest.raises(ValueError):
        jsr_source(scopes=[], packages=[])
