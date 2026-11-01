import sys
from pathlib import Path

# Add package root to sys.path
package_root = Path(__file__).parent.parent
sys.path.insert(0, str(package_root))
sys.path.insert(0, str(Path(__file__).parent))
