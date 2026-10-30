"""Pytest configuration for Spanner graph adapter tests."""

import pathlib
import sys

# Make packages/shared (contract_suite) importable.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "shared"))

pytest_plugins = ["pytest_asyncio"]
