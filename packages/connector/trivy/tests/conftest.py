"""Pytest fixtures for Trivy connector unit tests."""

import json
import os

import pytest


@pytest.fixture
def sample_trivy_json():
    """Load the fixture sample Trivy JSON report."""
    fixture_path = os.path.join(os.path.dirname(__file__), "sample_trivy_report.json")
    with open(fixture_path, encoding="utf-8") as f:
        return json.load(f)
