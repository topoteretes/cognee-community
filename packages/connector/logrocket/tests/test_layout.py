"""Keep the installed connector independent of checkout-only diagnostics."""

import subprocess
import sys
from pathlib import Path

import cognee_community_connector_logrocket as connector
from cognee_community_connector_logrocket import logrocket as implementation

ROOT = Path(__file__).resolve().parents[1]


def test_public_api_is_ingestion_only():
    assert connector.__all__ == ["logrocket_source", "remember_logrocket"]
    for name in ("main", "LogRocketRESTClient", "inspect_snapshot", "probe_tool"):
        assert not hasattr(implementation, name)
    assert not hasattr(implementation, "_CapabilitySnapshot")
    assert not hasattr(implementation._ToolDefinition, "to_dict")
    assert not hasattr(implementation._ToolDefinition, "from_dict")


def test_runtime_import_does_not_load_diagnostics():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import cognee_community_connector_logrocket; "
            "assert 'examples.diagnostics' not in sys.modules; "
            "assert 'diagnostics' not in sys.modules",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_checkout_diagnostic_command_works_offline():
    result = subprocess.run(
        [
            sys.executable,
            "examples/inspect_capabilities.py",
            "inspect-capabilities",
            "--from-file",
            "tests/fixtures/public-docs-baseline.json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Safe to implement sync from this evidence: NO" in result.stdout
