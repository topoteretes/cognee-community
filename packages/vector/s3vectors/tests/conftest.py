"""Shared pytest setup: make packages/shared (contract_suite) importable."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "shared"))
