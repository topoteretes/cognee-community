"""Unit tests for Jupyter notebook parser."""

import pytest
from cognee_community_connector_jupyter.jupyter import (
    _clean_source,
    _render_outputs,
    _strip_ansi,
    parse_notebook_content,
)


def test_clean_source_string():
    assert _clean_source("print('hello')") == "print('hello')"


def test_clean_source_list():
    assert _clean_source(["def foo():\n", "    return 42\n"]) == "def foo():\n    return 42\n"


def test_clean_source_invalid():
    assert _clean_source(None) == ""
    assert _clean_source(123) == ""


def test_strip_ansi():
    ansi_text = "\x1b[31mError:\x1b[0m division by zero"
    assert _strip_ansi(ansi_text) == "Error: division by zero"


def test_render_outputs_stream():
    outputs = [{"output_type": "stream", "name": "stdout", "text": ["Line 1\n", "Line 2\n"]}]
    rendered = _render_outputs(outputs)
    assert "[stdout]" in rendered
    assert "Line 1\nLine 2" in rendered


def test_render_outputs_execute_result():
    outputs = [
        {
            "output_type": "execute_result",
            "data": {"text/plain": "42", "image/png": "iVBORw0KGgoAAAANSUhEUgAA..."},
        }
    ]
    rendered = _render_outputs(outputs)
    assert "[output]" in rendered
    assert "42" in rendered
    assert "iVBORw0KGgoAAA" not in rendered  # Image binary must be skipped


def test_render_outputs_error_traceback():
    outputs = [
        {
            "output_type": "error",
            "ename": "ZeroDivisionError",
            "evalue": "division by zero",
            "traceback": ["\x1b[31mZeroDivisionError: division by zero\x1b[0m"],
        }
    ]
    rendered = _render_outputs(outputs)
    assert "[ZeroDivisionError]" in rendered
    assert "division by zero" in rendered
    assert "\x1b[" not in rendered


def test_render_outputs_max_chars_truncation():
    outputs = [{"output_type": "stream", "text": "A" * 5000}]
    rendered = _render_outputs(outputs, max_chars=100)
    assert len(rendered) <= 150
    assert "[output truncated]" in rendered


def test_parse_notebook_simple_markdown_and_code():
    nb = {
        "nbformat": 4,
        "metadata": {"language_info": {"name": "python"}},
        "cells": [
            {"cell_type": "markdown", "source": "# My Data Science Analysis\nHere is an explanation."},
            {"cell_type": "code", "source": "x = 10\nprint(x)", "outputs": []},
        ],
    }
    title, content = parse_notebook_content(nb)
    assert title == "My Data Science Analysis"
    assert "Here is an explanation." in content
    assert "```python\nx = 10\nprint(x)\n```" in content


def test_parse_notebook_json_string():
    import json

    nb_str = json.dumps(
        {
            "nbformat": 4,
            "cells": [
                {"cell_type": "markdown", "source": "# Analysis"},
                {"cell_type": "code", "source": "import numpy as np", "outputs": []},
            ],
        }
    )
    title, content = parse_notebook_content(nb_str)
    assert title == "Analysis"
    assert "import numpy as np" in content


def test_parse_notebook_with_outputs_enabled():
    nb = {
        "nbformat": 4,
        "cells": [
            {
                "cell_type": "code",
                "source": "print('hello')",
                "outputs": [{"output_type": "stream", "text": "hello\n"}],
            }
        ],
    }
    title, content = parse_notebook_content(nb, include_outputs=True)
    assert "```output" in content
    assert "[stdout]\nhello" in content


def test_parse_notebook_with_outputs_disabled():
    nb = {
        "nbformat": 4,
        "cells": [
            {
                "cell_type": "code",
                "source": "print('hello')",
                "outputs": [{"output_type": "stream", "text": "hello\n"}],
            }
        ],
    }
    title, content = parse_notebook_content(nb, include_outputs=False)
    assert "```output" not in content
    assert "[stdout]" not in content


def test_parse_notebook_v3_fallback():
    nb_v3 = {
        "nbformat": 3,
        "worksheets": [
            {
                "cells": [
                    {"cell_type": "markdown", "source": "Legacy notebook text"},
                    {"cell_type": "code", "input": "y = 20", "source": "y = 20"},
                ]
            }
        ],
    }
    title, content = parse_notebook_content(nb_v3, fallback_title="Legacy Notebook")
    assert title == "Legacy Notebook"
    assert "Legacy notebook text" in content
    assert "y = 20" in content


def test_parse_notebook_empty_cells():
    nb = {"nbformat": 4, "cells": []}
    title, content = parse_notebook_content(nb, fallback_title="Empty NB")
    assert title == "Empty NB"
    assert content == ""


def test_parse_notebook_invalid_input():
    with pytest.raises(ValueError, match="notebook_json must be a JSON string or dict"):
        parse_notebook_content(12345)  # type: ignore
