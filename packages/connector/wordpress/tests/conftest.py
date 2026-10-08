import sys
from pathlib import Path

# Make tests/fake_wordpress.py importable as a plain module.
sys.path.insert(0, str(Path(__file__).parent))
