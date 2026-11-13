"""Test configuration and path resolution for JSR connector tests."""

import os
import sys

# Ensure connector package is directly importable
connector_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if connector_root not in sys.path:
    sys.path.insert(0, connector_root)
