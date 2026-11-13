import json

import pytest

from cognee_community_connector_jupyter.jupyter import (
    JUPYTER_SOURCE_NAME,
    iter_notebook_cells,
    jupyter_notebook_source,
)

SAMPLE_NOTEBOOK = {
    "cells": [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": ["# Analysis of Q3 Metrics\n", "We observed an increase in retention."],
        },
        {
            "cell_type": "code",
            "execution_count": 1,
            "metadata": {},
            "outputs": [
                {
                    "name": "stdout",
                    "output_type": "stream",
                    "text": ["Loaded 1500 records\n"],
                },
                {
                    "data": {
                        "text/plain": ["<DataFrame shape=(1500, 4)>"],
                    },
                    "execution_count": 1,
                    "output_type": "execute_result",
                },
            ],
            "source": ["import pandas as pd\n", "df = pd.read_csv('metrics.csv')\n", "df"],
        },
    ],
    "metadata": {
        "language_info": {"name": "python"},
    },
    "nbformat": 4,
    "nbformat_minor": 2,
}


def test_parse_notebook_cells(tmp_path):
    nb_file = tmp_path / "eda.ipynb"
    nb_file.write_text(json.dumps(SAMPLE_NOTEBOOK), encoding="utf-8")

    cells = list(iter_notebook_cells(nb_file))
    assert len(cells) == 2

    # Markdown cell
    assert cells[0]["cell_type"] == "markdown"
    assert "Analysis of Q3 Metrics" in cells[0]["content"]

    # Code cell
    assert cells[1]["cell_type"] == "code"
    assert "import pandas as pd" in cells[1]["content"]
    assert "Loaded 1500 records" in cells[1]["content"]
    assert "<DataFrame shape=(1500, 4)>" in cells[1]["content"]


def test_recursive_directory_scan(tmp_path):
    sub = tmp_path / "nested"
    sub.mkdir()
    (tmp_path / "a.ipynb").write_text(json.dumps(SAMPLE_NOTEBOOK), encoding="utf-8")
    (sub / "b.ipynb").write_text(json.dumps(SAMPLE_NOTEBOOK), encoding="utf-8")

    cells = list(iter_notebook_cells(tmp_path))
    assert len(cells) == 4
    names = {c["notebook_name"] for c in cells}
    assert names == {"a", "b"}


def test_jupyter_source_dlt(tmp_path):
    dlt = pytest.importorskip("dlt")
    nb_file = tmp_path / "model.ipynb"
    nb_file.write_text(json.dumps(SAMPLE_NOTEBOOK), encoding="utf-8")

    db_path = tmp_path / "jupyter.db"
    pipeline = dlt.pipeline(
        pipeline_name="jupyter_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="jupyter_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    source = jupyter_notebook_source(nb_file)
    assert getattr(source, "cognee_document_source", None) == JUPYTER_SOURCE_NAME

    pipeline.run(source)
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT cell_type, title FROM jupyter_notebook_cells")
        assert len(rows) == 2
        types = {r[0] for r in rows}
        assert types == {"markdown", "code"}
