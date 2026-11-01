"""Check the Python compatibility range published in installed metadata."""

from importlib.metadata import metadata

import pytest
from packaging.specifiers import SpecifierSet


@pytest.mark.parametrize("version", ["3.11.0", "3.12.13", "3.13.0", "3.13.1", "3.13.9"])
def test_supported_python_releases_include_patch_versions(version):
    supported = SpecifierSet(metadata("cognee-community-connector-logrocket")["Requires-Python"])
    assert version in supported


@pytest.mark.parametrize("version", ["3.10.9", "3.14.0"])
def test_unsupported_python_releases_are_excluded(version):
    supported = SpecifierSet(metadata("cognee-community-connector-logrocket")["Requires-Python"])
    assert version not in supported
