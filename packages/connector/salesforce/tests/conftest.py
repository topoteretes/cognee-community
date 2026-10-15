import pathlib
import sys

# Ensure connector package is in python search path
package_root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(package_root))

# Also ensure cognee core repo is discoverable if present in workspace
try:
    core_cognee = pathlib.Path(__file__).resolve().parents[6] / "cognee" / "cognee"
    if core_cognee.exists():
        sys.path.insert(0, str(core_cognee))
except Exception:
    pass
