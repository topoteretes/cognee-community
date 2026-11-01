"""Unit tests for Jupyter file discovery and dlt source integration."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cognee_community_connector_jupyter.jupyter import (
    DOCUMENT_SOURCE_ATTR,
    JUPYTER_SOURCE_NAME,
    _find_notebook_files,
    jupyter_source,
)


def test_find_notebook_files(tmp_path: Path):
    nb1 = tmp_path / "notebook1.ipynb"
    nb2 = tmp_path / "notebook2.ipynb"
    txt = tmp_path / "notes.txt"
    sub = tmp_path / "sub"
    sub.mkdir()
    nb3 = sub / "nested.ipynb"

    nb1.write_text("{}", encoding="utf-8")
    nb2.write_text("{}", encoding="utf-8")
    txt.write_text("hello", encoding="utf-8")
    nb3.write_text("{}", encoding="utf-8")

    discovered = _find_notebook_files(workspace_dir=tmp_path)
    discovered_names = {p.name for p in discovered}

    assert "notebook1.ipynb" in discovered_names
    assert "notebook2.ipynb" in discovered_names
    assert "nested.ipynb" in discovered_names
    assert "notes.txt" not in discovered_names


def test_find_notebook_files_explicit_paths(tmp_path: Path):
    nb1 = tmp_path / "a.ipynb"
    nb2 = tmp_path / "b.ipynb"
    nb1.write_text("{}", encoding="utf-8")
    nb2.write_text("{}", encoding="utf-8")

    # Pass only a.ipynb
    found = _find_notebook_files(notebook_paths=[nb1])
    assert len(found) == 1
    assert found[0].name == "a.ipynb"


def test_find_notebook_files_deduplication(tmp_path: Path):
    nb1 = tmp_path / "dup.ipynb"
    nb1.write_text("{}", encoding="utf-8")

    # Pass same file twice
    found = _find_notebook_files(notebook_paths=[nb1, nb1], workspace_dir=tmp_path)
    assert len(found) == 1


def test_jupyter_source_requires_dlt_error():
    with (
        patch.dict("sys.modules", {"dlt": None}),
        pytest.raises(ImportError, match="The Jupyter connector requires dlt"),
    ):
        jupyter_source()


def test_jupyter_source_decorations(tmp_path: Path):
    # Mock dlt module to test source creation and Cognee document mode attribute
    mock_dlt = MagicMock()
    mock_source_obj = MagicMock()
    mock_dlt.source.return_value = lambda func: mock_source_obj

    with patch.dict("sys.modules", {"dlt": mock_dlt}):
        source = jupyter_source(workspace_dir=tmp_path)
        assert getattr(source, DOCUMENT_SOURCE_ATTR) == JUPYTER_SOURCE_NAME


def test_notebook_row_generation(tmp_path: Path):
    nb_file = tmp_path / "test_eda.ipynb"
    nb_content = {
        "nbformat": 4,
        "cells": [
            {
                "cell_type": "markdown",
                "source": "# Exploratory Data Analysis\nThis is a walkthrough.",
            },
            {
                "cell_type": "code",
                "source": "df.head()",
                "outputs": [{"output_type": "stream", "text": "col1 col2\n1 2\n"}],
            },
        ],
    }
    nb_file.write_text(json.dumps(nb_content), encoding="utf-8")

    # Verify discovery and generator
    files = _find_notebook_files(workspace_dir=tmp_path)
    assert len(files) == 1

    # Check parsing output directly
    from cognee_community_connector_jupyter.jupyter import parse_notebook_content

    title, content = parse_notebook_content(nb_file.read_text(encoding="utf-8"))
    assert title == "Exploratory Data Analysis"
    assert "df.head()" in content
    assert "[stdout]" in content
