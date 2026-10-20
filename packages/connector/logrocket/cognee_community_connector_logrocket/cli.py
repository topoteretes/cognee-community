"""Command-line entry point for the LogRocket connector capability spike."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from cognee_community_connector_logrocket.capabilities import inspect_snapshot
from cognee_community_connector_logrocket.client import CapabilitySnapshot, LogRocketMCPClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cognee-logrocket")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser(
        "inspect-capabilities",
        help="Capture and assess LogRocket MCP tool schemas without calling data tools.",
    )
    source = inspect.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-file",
        type=Path,
        help="Inspect a previously captured JSON snapshot entirely offline.",
    )
    source.add_argument("--organization", help="LogRocket organization ID for live inspection.")
    inspect.add_argument("--project", help="LogRocket project ID for live inspection.")
    inspect.add_argument("--api-key-env", default="LOGROCKET_API_KEY")
    inspect.add_argument(
        "--output",
        type=Path,
        help="Write the complete capability snapshot (including raw schemas) to this file.",
    )
    inspect.add_argument("--json", action="store_true", help="Print the assessment as JSON.")
    inspect.add_argument(
        "--require-sync-ready",
        action="store_true",
        help="Exit 2 unless pagination, stable IDs, and deletion visibility are proven.",
    )
    probe = subparsers.add_parser(
        "probe-response", help="Read at most five search results and print only their structure."
    )
    probe.add_argument("--organization", required=True)
    probe.add_argument("--project", required=True)
    probe.add_argument("--api-key-env", default="LOGROCKET_API_KEY")
    probe.add_argument("--baseline", required=True, type=Path)
    probe.add_argument("--tool", choices=["find_sessions", "find_issues"], required=True)
    probe.add_argument("--arguments-file", required=True, type=Path)
    rest = subparsers.add_parser(
        "probe-rest",
        help="Read documented audit/export pages without modifying LogRocket or Cognee.",
    )
    rest.add_argument("--organization", required=True)
    rest.add_argument("--project", required=True)
    rest.add_argument("--api-key-env", default="LOGROCKET_API_KEY")
    rest.add_argument("--resource", choices=["audit", "session-exports"], required=True)
    position = rest.add_mutually_exclusive_group()
    position.add_argument("--cursor", help="Opaque cursor returned by the previous page.")
    position.add_argument("--since-ms", type=int, help="Initial Unix timestamp in milliseconds.")
    rest.add_argument("--limit", type=int, default=2)
    rest.add_argument("--max-pages", type=int, default=1)
    return parser


def _read_snapshot(path: Path) -> CapabilitySnapshot:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Capability snapshot root must be a JSON object.")
    return CapabilitySnapshot.from_dict(value)


def _write_snapshot(path: Path, snapshot: CapabilitySnapshot) -> None:
    path.write_text(
        json.dumps(snapshot.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _format_report(report) -> str:
    lines = [
        f"Endpoint: {report.endpoint}",
        f"Protocol: {report.protocol_version or 'not reported'}",
    ]
    for tool in report.tools:
        lines.append(
            f"{tool.name}: present={tool.present}, output_schema={tool.response_schema}, "
            f"pagination={tool.pagination}, stable_identity={tool.stable_identity}, "
            f"deletion_visibility={tool.deletion_visibility}"
        )
    verdict = "YES" if report.sync_implementation_allowed else "NO"
    lines.append(f"Safe to implement sync from this evidence: {verdict}")
    if report.blockers:
        lines.append("Blockers:")
        lines.extend(f"- {blocker}" for blocker in report.blockers)
    return "\n".join(lines)


async def _live_snapshot(args: argparse.Namespace) -> CapabilitySnapshot:
    if not args.project:
        raise ValueError("--project is required with --organization.")
    client = LogRocketMCPClient(
        organization_id=args.organization,
        project_id=args.project,
        api_key_env=args.api_key_env,
    )
    return await client.inspect_capabilities()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "probe-rest":
            from cognee_community_connector_logrocket.rest import LogRocketRESTClient

            client = LogRocketRESTClient(
                args.organization, args.project, api_key_env=args.api_key_env
            )
            result = asyncio.run(
                client.probe(
                    args.resource,
                    cursor=args.cursor,
                    since_ms=args.since_ms,
                    limit=args.limit,
                    max_pages=args.max_pages,
                )
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["status"] == "ok" else 1
        if args.command == "probe-response":
            arguments = json.loads(args.arguments_file.read_text(encoding="utf-8"))
            if not isinstance(arguments, dict):
                raise ValueError("Probe arguments must be a JSON object.")
            client = LogRocketMCPClient(
                args.organization, args.project, api_key_env=args.api_key_env
            )
            result = asyncio.run(
                client.probe(args.tool, arguments, baseline=_read_snapshot(args.baseline))
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        snapshot = (
            _read_snapshot(args.from_file)
            if args.from_file is not None
            else asyncio.run(_live_snapshot(args))
        )
        if args.output is not None:
            _write_snapshot(args.output, snapshot)
        report = inspect_snapshot(snapshot)
        print(
            json.dumps(report.to_dict(), indent=2, sort_keys=True)
            if args.json
            else _format_report(report)
        )
        return 2 if args.require_sync_ready and not report.sync_implementation_allowed else 0
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"Capability inspection failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
