"""Make the shared offline adapter contracts available to this package."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "shared"))
