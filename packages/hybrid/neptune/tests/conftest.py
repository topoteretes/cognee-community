"""Shared pytest setup: make packages/shared (contract_suite) importable."""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "shared"))

# The scenario scripts directly under tests/ connect to a real Neptune Analytics
# graph when imported. Without GRAPH_ID there is nothing to connect to, so leave
# them out of collection instead of failing it.
collect_ignore_glob = [] if os.getenv("GRAPH_ID") else ["test_neptune_analytics_*.py"]
