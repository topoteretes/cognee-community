"""Unit tests for the PyPI exact-release metadata connector."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cognee_community_connector_pypi import (
    DOCUMENT_SOURCE_ATTR,
    PYPI_SOURCE_NAME,
    PyPIPackageNotFoundError,
    PyPIValidationError,
    fetch_pypi_release,
    normalize_project_name,
    parse_release_spec,
    pypi_source,
    render_release_document,
)

SAMPLE_RELEASE_PAYLOAD: dict[str, Any] = {
    "info": {
        "name": "sample-pkg",
        "version": "1.0.0",
        "summary": "A sample library for testing",
        "description": (
            "Long markdown description here.\n\n### Usage\n```python\nimport sample\n```"
        ),
        "requires_python": ">=3.10",
        "requires_dist": [
            "httpx>=0.27.0",
            "pydantic>=2.0; extra == 'full'",
        ],
        "classifiers": [
            "Programming Language :: Python :: 3",
            "License :: OSI Approved :: MIT License",
        ],
        "license": "MIT",
        "author": "Alice Developer",
        "maintainer": "Bob Maintainer",
        "home_page": "https://example.com/sample-pkg",
        "project_urls": {
            "Homepage": "https://example.com/sample-pkg",
            "Repository": "https://github.com/example/sample-pkg",
            "Documentation": "https://docs.example.com",
        },
        "yanked": False,
        "yanked_reason": None,
    },
    "urls": [
        {
            "filename": "sample_pkg-1.0.0-py3-none-any.whl",
            "packagetype": "bdist_wheel",
            "size": 12345,
            "upload_time_iso_8601": "2024-05-01T12:00:00Z",
            "digests": {
                "sha256": "abcd1234ef567890abcd1234ef567890abcd1234ef567890abcd1234ef567890"
            },
            "yanked": False,
            "yanked_reason": None,
        },
        {
            "filename": "sample_pkg-1.0.0.tar.gz",
            "packagetype": "sdist",
            "size": 9876,
            "upload_time_iso_8601": "2024-05-01T11:55:00Z",
            "digests": {
                "sha256": "9876ef56abcd12349876ef56abcd12349876ef56abcd12349876ef56abcd1234"
            },
            "yanked": False,
            "yanked_reason": None,
        },
    ],
}

YANKED_RELEASE_PAYLOAD: dict[str, Any] = {
    "info": {
        "name": "broken-pkg",
        "version": "2.0.0",
        "summary": "This release had a critical flaw",
        "description": "Do not use.",
        "requires_python": ">=3.9",
        "requires_dist": [],
        "classifiers": [],
        "license": "Apache-2.0",
        "yanked": True,
        "yanked_reason": "Contains critical regression in authentication module",
    },
    "urls": [
        {
            "filename": "broken_pkg-2.0.0-py3-none-any.whl",
            "packagetype": "bdist_wheel",
            "size": 5000,
            "upload_time_iso_8601": "2024-06-01T10:00:00Z",
            "digests": {
                "sha256": "ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000ffff0000"
            },
            "yanked": True,
            "yanked_reason": "Contains critical regression in authentication module",
        }
    ],
}


def _make_mock_client(responses: dict[str, tuple[int, dict[str, Any]]]) -> httpx.Client:
    """Create an httpx.Client with MockTransport mapping URL paths to (status_code, json_dict)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in responses:
            status_code, body = responses[path]
            return httpx.Response(status_code, json=body, request=request)
        return httpx.Response(404, json={"message": "Not Found"}, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- Tests for Project Name Normalization (PEP 503) ---


def test_normalize_project_name_pep503():
    """Verify PyPA standard normalization handles hyphens, underscores, dots, and casing."""
    assert normalize_project_name("requests-toolbelt") == "requests-toolbelt"
    assert normalize_project_name("requests_toolbelt") == "requests-toolbelt"
    assert normalize_project_name("Requests.Toolbelt") == "requests-toolbelt"
    assert normalize_project_name("REQUESTS---TOOLBELT") == "requests-toolbelt"
    assert normalize_project_name("Friendly_Bard.Service") == "friendly-bard-service"


# --- Tests for Release Spec Parsing ---


def test_parse_release_spec_tuple():
    assert parse_release_spec(("cognee", "1.6.3")) == ("cognee", "1.6.3")
    assert parse_release_spec(("Cognee_Community", "0.1.0")) == ("cognee-community", "0.1.0")


def test_parse_release_spec_strings():
    assert parse_release_spec("requests==2.31.0") == ("requests", "2.31.0")
    assert parse_release_spec("requests_toolbelt==0.10.1") == ("requests-toolbelt", "0.10.1")
    assert parse_release_spec("packaging@24.2") == ("packaging", "24.2")


def test_parse_release_spec_invalid():
    with pytest.raises(ValueError, match="must use 'project==version'"):
        parse_release_spec("requests>=2.31.0")

    with pytest.raises(ValueError, match="Expected a"):
        parse_release_spec(("one", "two", "three"))

    with pytest.raises(TypeError):
        parse_release_spec(123)  # type: ignore[arg-type]


# --- Tests for Metadata Fetching and Identity Validation ---


def test_fetch_pypi_release_success():
    client = _make_mock_client({"/pypi/sample-pkg/1.0.0/json": (200, SAMPLE_RELEASE_PAYLOAD)})
    data = fetch_pypi_release("sample-pkg", "1.0.0", client=client)
    assert data["info"]["name"] == "sample-pkg"
    assert data["info"]["version"] == "1.0.0"


def test_fetch_pypi_release_name_normalization_url():
    """Unnormalized name 'Sample_Pkg' should query canonical '/pypi/sample-pkg/...'."""
    client = _make_mock_client({"/pypi/sample-pkg/1.0.0/json": (200, SAMPLE_RELEASE_PAYLOAD)})
    data = fetch_pypi_release("Sample_Pkg", "1.0.0", client=client)
    assert data["info"]["version"] == "1.0.0"


def test_fetch_pypi_release_not_found_404():
    client = _make_mock_client({})
    with pytest.raises(PyPIPackageNotFoundError, match="not found on PyPI"):
        fetch_pypi_release("nonexistent", "9.9.9", client=client)


def test_fetch_pypi_release_identity_mismatch_project():
    mismatched_payload = {
        "info": {"name": "other-pkg", "version": "1.0.0"},
        "urls": [],
    }
    client = _make_mock_client({"/pypi/sample-pkg/1.0.0/json": (200, mismatched_payload)})
    with pytest.raises(PyPIValidationError, match="PyPI returned project name 'other-pkg'"):
        fetch_pypi_release("sample-pkg", "1.0.0", client=client)


def test_fetch_pypi_release_identity_mismatch_version():
    mismatched_payload = {
        "info": {"name": "sample-pkg", "version": "9.9.9"},
        "urls": [],
    }
    client = _make_mock_client({"/pypi/sample-pkg/1.0.0/json": (200, mismatched_payload)})
    with pytest.raises(PyPIValidationError, match=r"PyPI returned version '9\.9\.9'"):
        fetch_pypi_release("sample-pkg", "1.0.0", client=client)


# --- Tests for Document Rendering ---


def test_render_release_document_fields():
    doc = render_release_document("Sample_Pkg", "1.0.0", SAMPLE_RELEASE_PAYLOAD)

    assert doc["id"] == "pypi:sample-pkg:1.0.0"
    assert doc["title"] == "PyPI Package: sample-pkg==1.0.0"
    assert doc["project"] == "sample-pkg"
    assert doc["version"] == "1.0.0"
    assert doc["summary"] == "A sample library for testing"
    assert doc["requires_python"] == ">=3.10"
    assert "httpx>=0.27.0" in doc["dependencies"]
    assert doc["yanked"] is False
    assert doc["yanked_reason"] is None
    assert doc["license"] == "MIT"
    assert len(doc["files"]) == 2

    content = doc["content"]
    assert "# PyPI Package: sample-pkg (Version: 1.0.0)" in content
    assert "**Summary**: A sample library for testing" in content
    assert "**Requires-Python**: `>=3.10`" in content
    assert "httpx>=0.27.0" in content
    assert "sample_pkg-1.0.0-py3-none-any.whl" in content
    assert "abcd1234ef567890abcd1234ef567890abcd1234ef567890abcd1234ef567890" in content


def test_render_release_document_yanked_warning():
    doc = render_release_document("broken-pkg", "2.0.0", YANKED_RELEASE_PAYLOAD)

    assert doc["yanked"] is True
    assert doc["yanked_reason"] == "Contains critical regression in authentication module"

    content = doc["content"]
    assert "WARNING: YANKED RELEASE" in content
    assert "Contains critical regression in authentication module" in content


def test_render_release_document_nullable_fields():
    minimal_payload = {
        "info": {
            "name": "bare-pkg",
            "version": "0.0.1",
        },
        "urls": [],
    }
    doc = render_release_document("bare-pkg", "0.0.1", minimal_payload)

    assert doc["id"] == "pypi:bare-pkg:0.0.1"
    assert doc["summary"] == ""
    assert doc["requires_python"] == ""
    assert doc["dependencies"] == []
    assert doc["yanked"] is False
    assert doc["files"] == []
    assert "# PyPI Package: bare-pkg (Version: 0.0.1)" in doc["content"]


# --- Tests for DLT Resource and Document Mode ---


def test_pypi_source_dlt_resource_configuration():
    pytest.importorskip("dlt")

    client = _make_mock_client(
        {
            "/pypi/sample-pkg/1.0.0/json": (200, SAMPLE_RELEASE_PAYLOAD),
            "/pypi/broken-pkg/2.0.0/json": (200, YANKED_RELEASE_PAYLOAD),
        }
    )

    source = pypi_source(
        [("sample-pkg", "1.0.0"), "broken-pkg==2.0.0"],
        client=client,
    )

    # Document mode check
    assert getattr(source, DOCUMENT_SOURCE_ATTR) == PYPI_SOURCE_NAME
    assert source.write_disposition == "replace"
    assert source.name == "pypi_releases"

    # Evaluate generator and check yielded rows
    rows = list(source)
    assert len(rows) == 2
    assert rows[0]["id"] == "pypi:sample-pkg:1.0.0"
    assert rows[1]["id"] == "pypi:broken-pkg:2.0.0"
    assert rows[1]["yanked"] is True


def test_pypi_source_aborts_on_invalid_release():
    """Ensure that if one release in the list fails, the sync fails before yielding rows."""
    pytest.importorskip("dlt")

    client = _make_mock_client(
        {
            "/pypi/sample-pkg/1.0.0/json": (200, SAMPLE_RELEASE_PAYLOAD),
        }
    )

    source = pypi_source(
        [("sample-pkg", "1.0.0"), ("nonexistent", "9.9.9")],
        client=client,
    )

    with pytest.raises(Exception) as exc_info:
        list(source)

    assert "not found on PyPI" in str(exc_info.value)
