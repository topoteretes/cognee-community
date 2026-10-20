import json
from pathlib import Path

from cognee_community_connector_logrocket.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "public-docs-baseline.json"
LIVE_FIXTURE = Path(__file__).parent / "fixtures" / "live-required-tool-schemas-2026-10-16.json"


def test_offline_capability_command_reports_blockers_without_api_key(capsys):
    exit_code = main(["inspect-capabilities", "--from-file", str(FIXTURE)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Safe to implement sync from this evidence: NO" in output
    assert "no MCP outputSchema" in output


def test_offline_capability_command_can_enforce_readiness(capsys):
    exit_code = main(
        [
            "inspect-capabilities",
            "--from-file",
            str(FIXTURE),
            "--require-sync-ready",
            "--json",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert output["sync_implementation_allowed"] is False


def test_live_mode_requires_project_before_connecting(capsys):
    exit_code = main(["inspect-capabilities", "--organization", "org"])

    assert exit_code == 1
    assert "--project is required" in capsys.readouterr().out


def test_sanitized_live_schema_capture_confirms_limit_without_pagination(capsys):
    exit_code = main(["inspect-capabilities", "--from-file", str(LIVE_FIXTURE), "--json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["sync_implementation_allowed"] is False
    assert [tool["pagination"] for tool in output["tools"]] == ["partial", "partial"]
    assert all(tool["pagination_evidence"] == ["input:limit"] for tool in output["tools"])
    assert all(tool["response_schema"] is False for tool in output["tools"])


def test_malformed_tool_snapshot_returns_error(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"tools": [None]}))
    assert main(["inspect-capabilities", "--from-file", str(path)]) == 1
    assert "must be an object" in capsys.readouterr().out


def test_probe_cli_prints_structure_report(tmp_path, monkeypatch, capsys):
    from cognee_community_connector_logrocket.client import LogRocketMCPClient

    arguments = tmp_path / "arguments.json"
    arguments.write_text(json.dumps({"query": "test", "limit": 1}))

    async def fake_probe(self, name, args, *, baseline):
        assert name == "find_sessions"
        assert args == {"query": "test", "limit": 1}
        assert baseline.tools
        return {"sync_implementation_allowed": False, "response_shape": {"type": "object"}}

    monkeypatch.setattr(LogRocketMCPClient, "probe", fake_probe)
    assert (
        main(
            [
                "probe-response",
                "--organization",
                "org",
                "--project",
                "project",
                "--baseline",
                str(LIVE_FIXTURE),
                "--tool",
                "find_sessions",
                "--arguments-file",
                str(arguments),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["sync_implementation_allowed"] is False


def test_rest_probe_cli_forwards_bounded_read_parameters(monkeypatch, capsys):
    from cognee_community_connector_logrocket.rest import LogRocketRESTClient

    async def fake_probe(self, resource, **kwargs):
        assert resource == "audit"
        assert kwargs == {"cursor": None, "since_ms": 1700, "limit": 2, "max_pages": 3}
        return {"status": "ok", "automatic_deletion_supported": False}

    monkeypatch.setattr(LogRocketRESTClient, "probe", fake_probe)
    assert (
        main(
            [
                "probe-rest",
                "--organization",
                "org",
                "--project",
                "project",
                "--resource",
                "audit",
                "--since-ms",
                "1700",
                "--max-pages",
                "3",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["automatic_deletion_supported"] is False


def test_rest_probe_cli_reports_safe_failure(monkeypatch, capsys):
    from cognee_community_connector_logrocket.rest import LogRocketRESTClient

    async def fake_probe(self, resource, **kwargs):
        return {"status": "error", "error_code": "http_failure", "http_status": 403}

    monkeypatch.setattr(LogRocketRESTClient, "probe", fake_probe)
    assert (
        main(
            [
                "probe-rest",
                "--organization",
                "org",
                "--project",
                "project",
                "--resource",
                "audit",
                "--since-ms",
                "0",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["http_status"] == 403


def test_rest_probe_cli_rejects_unbounded_request(capsys):
    assert (
        main(
            [
                "probe-rest",
                "--organization",
                "org",
                "--project",
                "project",
                "--resource",
                "audit",
                "--since-ms",
                "0",
                "--limit",
                "100",
            ]
        )
        == 1
    )
    assert "between 1 and 5" in capsys.readouterr().out
