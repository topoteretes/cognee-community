"""Unit tests for the PyPI Data-Source Connector."""

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch
import pytest

from cognee_community_connector_pypi.pypi import (
    PyPIConnector,
    PyPIPackageDocument,
    fetch_package_metadata,
    pypi_source,
)

SAMPLE_PYPI_PAYLOAD = {
    "info": {
        "name": "fastapi",
        "version": "0.110.0",
        "summary": "FastAPI framework, high performance, easy to learn, fast to code",
        "author": "Tiangolo",
        "author_email": "tiangolo@example.com",
        "license": "MIT",
        "home_page": "https://fastapi.tiangolo.com/",
        "project_urls": {
            "Documentation": "https://fastapi.tiangolo.com/",
            "Repository": "https://github.com/tiangolo/fastapi",
        },
        "requires_python": ">=3.8",
        "requires_dist": ["starlette>=0.37.2", "pydantic>=1.7.4"],
        "keywords": "api, framework, json, rest",
        "description": "FastAPI is a modern, fast web framework for building APIs with Python.",
    }
}


def _create_mock_response(status_code: int = 200, payload: dict = None):
    mock = MagicMock()
    content = json.dumps(payload or SAMPLE_PYPI_PAYLOAD).encode("utf-8")
    mock.read.return_value = content
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = None
    return mock


def test_fetch_package_metadata_success():
    mock_resp = _create_mock_response(200, SAMPLE_PYPI_PAYLOAD)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        pkg = fetch_package_metadata("fastapi")

        assert pkg.name == "fastapi"
        assert pkg.version == "0.110.0"
        assert pkg.author == "Tiangolo"
        assert pkg.license == "MIT"
        assert "starlette>=0.37.2" in pkg.requires_dist
        assert pkg.requires_python == ">=3.8"


def test_fetch_package_metadata_empty_name():
    with pytest.raises(ValueError, match="Package name cannot be empty"):
        fetch_package_metadata("   ")


def test_fetch_package_metadata_not_found():
    http_error = urllib.error.HTTPError(
        url="https://pypi.org/pypi/invalid_pkg/json",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=io.BytesIO(b"Not Found"),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(ValueError, match="was not found on PyPI"):
            fetch_package_metadata("invalid_pkg")


def test_fetch_package_metadata_server_error():
    http_error = urllib.error.HTTPError(
        url="https://pypi.org/pypi/fastapi/json",
        code=500,
        msg="Internal Server Error",
        hdrs={},
        fp=io.BytesIO(b"Error"),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(RuntimeError, match="PyPI API returned HTTP 500"):
            fetch_package_metadata("fastapi")


def test_package_document_to_markdown():
    pkg = PyPIPackageDocument(
        name="test-pkg",
        version="1.2.3",
        summary="A test package",
        author="Tester",
        license="Apache-2.0",
        project_urls={"Docs": "https://example.com/docs"},
        requires_dist=["requests>=2.0"],
        description="Full documentation content goes here.",
    )

    markdown = pkg.to_markdown()
    assert "# test-pkg (v1.2.3)" in markdown
    assert "**Summary**: A test package" in markdown
    assert "**License**: Apache-2.0" in markdown
    assert "- **Docs**: https://example.com/docs" in markdown
    assert "- `requests>=2.0`" in markdown
    assert "Full documentation content goes here." in markdown


def test_package_document_to_cognee_document():
    pkg = PyPIPackageDocument(
        name="test-pkg",
        version="1.2.3",
        summary="Summary test",
        description="Docs body",
    )

    doc = pkg.to_cognee_document()
    assert doc["id"] == "pypi_test_pkg_1.2.3"
    assert doc["title"] == "PyPI: test-pkg 1.2.3"
    assert doc["metadata"]["source"] == "pypi"
    assert doc["metadata"]["package_name"] == "test-pkg"
    assert doc["metadata"]["version"] == "1.2.3"
    assert "Summary test" in doc["text"]


def test_connector_batch_documents():
    connector = PyPIConnector()
    mock_resp = _create_mock_response(200, SAMPLE_PYPI_PAYLOAD)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        docs = connector.fetch_documents(["fastapi", "pydantic"])
        assert len(docs) == 2
        assert docs[0]["metadata"]["package_name"] == "fastapi"
        assert docs[1]["metadata"]["package_name"] == "fastapi"


def test_connector_missing_package_handling():
    connector = PyPIConnector()
    http_error_404 = urllib.error.HTTPError(
        url="https://pypi.org/pypi/missing/json",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=io.BytesIO(b"Not Found"),
    )

    with patch("urllib.request.urlopen", side_effect=http_error_404):
        # When ignore_missing is True, should not raise
        results = connector.get_packages(["missing"], ignore_missing=True)
        assert results == []

        # When ignore_missing is False, should raise ValueError
        with pytest.raises(ValueError):
            connector.get_packages(["missing"], ignore_missing=False)


def test_pypi_source_generator():
    mock_resp = _create_mock_response(200, SAMPLE_PYPI_PAYLOAD)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        items = list(pypi_source(["fastapi"]))
        assert len(items) == 1
        assert items[0]["metadata"]["package_name"] == "fastapi"
