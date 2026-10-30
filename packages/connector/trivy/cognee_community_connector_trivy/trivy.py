"""Trivy connector for cognee: a dlt resource that syncs security scan reports into memory.

It ingests Trivy container image, Git repository, and filesystem security audit reports
(Vulnerabilities, Misconfigurations, and Secrets with automatic credential redaction)
into structured Markdown documents, enabling Cognee AI agents to query software security
posture, open CVEs, and compliance drift.

Usage with ``cognee.remember``::

    await cognee.remember(
        trivy_source(report_path="trivy-scan.json"),
        dataset_name="security_posture",
        primary_key="id",
        write_disposition="merge",
    )
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urljoin

import dlt
import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
from dlt.common.normalizers.naming.snake_case import NamingConvention

logger = get_logger("trivy_connector")

TRIVY_TABLE_NAME = "trivy_documents"
TRIVY_SOURCE_NAME = "trivy"
KINDS = ("vulnerabilities", "misconfigurations", "secrets", "summary")
DEFAULT_TIMEOUT = (10, 30)


class TrivySourceError(RuntimeError):
    """Base exception for Trivy connector errors."""


class TrivyAPIError(TrivySourceError):
    """Trivy server returned an error or could not be reached."""


class TrivyFormatError(TrivySourceError):
    """The provided Trivy report format or JSON schema is invalid."""


def _hash(value: Any) -> str:
    """Compute a deterministic short SHA-256 hash for fingerprinting."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _text(value: Any) -> str:
    """Return non-empty stripped string or empty string."""
    if value is None:
        return ""
    return str(value).strip()


def _sanitize_id(value: str) -> str:
    """Sanitize identifiers to prevent collision or invalid URI characters."""
    return re.sub(r"[^a-zA-Z0-9_\-\.:]", "_", value)


def _redact_secret_text(secret_text: str) -> str:
    """Redact raw credential bytes from being stored into knowledge graph vectors."""
    if not secret_text:
        return "[REDACTED]"
    if len(secret_text) <= 6:
        return "[REDACTED]"
    return f"{secret_text[:2]}...[REDACTED]...{secret_text[-2:]}"


# ---------------------------------------------------------------------------
# Trivy Client (for optional Trivy server mode)
# ---------------------------------------------------------------------------


class TrivyClient:
    """Client communicating with a live Trivy server instance."""

    def __init__(
        self,
        server_url: str = "http://localhost:4954",
        *,
        token: str | None = None,
        session: requests.Session | Any | None = None,
        timeout: tuple[int, int] = DEFAULT_TIMEOUT,
        check_active: Callable[[], None] | None = None,
    ):
        self.server_url = server_url.rstrip("/")
        self.token = token or os.environ.get("TRIVY_TOKEN")
        self.timeout = timeout
        self.check_active = check_active
        self._external_session = session is not None
        self.session = session if session is not None else requests.Session()

    def close(self) -> None:
        if not self._external_session and hasattr(self.session, "close"):
            self.session.close()

    def scan_image(self, image_name: str) -> dict[str, Any]:
        """Request a scan report for an image from the Trivy server."""
        if self.check_active is not None:
            self.check_active()

        url = urljoin(self.server_url + "/", "twirp/trivy.server.scanner.v1.Scanner/Scan")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Trivy-Token"] = self.token

        try:
            resp = self.session.post(
                url,
                json={"target": image_name, "artifact_type": "container_image"},
                headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise TrivyAPIError(f"Failed to query Trivy server at {url}: {exc}") from exc


# ---------------------------------------------------------------------------
# Report Parser & Normalizer
# ---------------------------------------------------------------------------


class TrivyReportParser:
    """Parses and normalizes Trivy JSON scan schemas (v1 and v2)."""

    @staticmethod
    def load_report(source: str | dict[str, Any]) -> dict[str, Any]:
        """Load report from file path, JSON string, or dict."""
        if isinstance(source, dict):
            return source
        if isinstance(source, str):
            if os.path.isfile(source):
                with open(source, encoding="utf-8") as f:
                    return json.load(f)
            try:
                return json.loads(source)
            except json.JSONDecodeError as exc:
                raise TrivyFormatError(f"Failed to parse Trivy report JSON: {exc}") from exc
        raise TrivyFormatError(f"Unsupported report source type: {type(source)}")


# ---------------------------------------------------------------------------
# Document Renderers (Markdown documents for Cognee Graph reasoning)
# ---------------------------------------------------------------------------


def render_vulnerability(
    artifact_name: str, target_name: str, vuln: dict[str, Any]
) -> dict[str, Any]:
    """Render a CVE vulnerability into a structured Markdown document."""
    cve_id = _text(vuln.get("VulnerabilityID")) or "UNKNOWN-CVE"
    pkg_name = _text(vuln.get("PkgName")) or "unknown-pkg"
    installed_ver = _text(vuln.get("InstalledVersion")) or "unknown"
    fixed_ver = _text(vuln.get("FixedVersion")) or "None (unpatched)"
    severity = _text(vuln.get("Severity")) or "UNKNOWN"
    title = _text(vuln.get("Title"))
    desc = _text(vuln.get("Description"))
    primary_url = _text(vuln.get("PrimaryURL"))
    cvss = vuln.get("CVSS") or {}

    lines = [
        f"# Trivy Vulnerability: {cve_id} in {pkg_name}",
        f"**Severity:** {severity}",
        f"**Target Artifact:** `{artifact_name}`",
        f"**Scanned Target Component:** `{target_name}`",
        f"**Vulnerable Package:** `{pkg_name}`",
        f"**Installed Version:** `{installed_ver}`",
        f"**Fixed Version:** `{fixed_ver}`",
    ]

    if title:
        lines.append(f"**Vulnerability Title:** {title}")

    if cvss and isinstance(cvss, dict):
        scores = []
        for vendor, details in cvss.items():
            if isinstance(details, dict) and "V3Score" in details:
                scores.append(f"{vendor} V3: {details['V3Score']}")
        if scores:
            lines.append(f"**CVSS Scores:** {', '.join(scores)}")

    if primary_url:
        lines.append(f"**Advisory URL:** [{cve_id}]({primary_url})")

    if desc:
        lines.append(f"\n### Description:\n{desc}")

    unique_key = _sanitize_id(f"{artifact_name}:{target_name}:{cve_id}:{pkg_name}")
    return {
        "id": f"trivy:vuln:{unique_key}",
        "title": f"Trivy Vulnerability: {cve_id} ({pkg_name} - {severity})",
        "content": "\n".join(lines),
        "url": primary_url or f"https://cve.mitre.org/cgi-bin/cvename.cgi?name={cve_id}",
        "_deleted": False,
    }


def render_misconfiguration(
    artifact_name: str, target_name: str, misconfig: dict[str, Any]
) -> dict[str, Any]:
    """Render a security misconfiguration into a structured Markdown document."""
    rule_id = _text(misconfig.get("ID")) or "UNKNOWN-RULE"
    title = _text(misconfig.get("Title")) or "Unnamed check"
    desc = _text(misconfig.get("Description"))
    severity = _text(misconfig.get("Severity")) or "UNKNOWN"
    resolution = _text(misconfig.get("Resolution"))
    check_type = _text(misconfig.get("Type"))
    status = _text(misconfig.get("Status")) or "FAIL"
    primary_url = _text(misconfig.get("PrimaryURL"))

    lines = [
        f"# Trivy Misconfiguration: {rule_id} — {title}",
        f"**Severity:** {severity}",
        f"**Check Status:** {status}",
        f"**Target Artifact:** `{artifact_name}`",
        f"**Target Component:** `{target_name}`",
        f"**Rule ID:** `{rule_id}`",
    ]

    if check_type:
        lines.append(f"**Check Type:** {check_type}")

    if resolution:
        lines.append(f"**Recommended Remediation:** {resolution}")

    if desc:
        lines.append(f"\n### Details:\n{desc}")

    unique_key = _sanitize_id(f"{artifact_name}:{target_name}:{rule_id}")
    return {
        "id": f"trivy:misconfig:{unique_key}",
        "title": f"Trivy Misconfiguration: {rule_id} ({severity})",
        "content": "\n".join(lines),
        "url": primary_url or f"https://avd.aquasec.com/misconfig/{rule_id.lower()}",
        "_deleted": False,
    }


def render_secret(artifact_name: str, target_name: str, secret: dict[str, Any]) -> dict[str, Any]:
    """Render a secret detection into Markdown with strictly redacted secret content."""
    rule_id = _text(secret.get("RuleID")) or "secret-detection"
    title = _text(secret.get("Title")) or "Hardcoded Secret Detected"
    category = _text(secret.get("Category")) or "Credential"
    severity = _text(secret.get("Severity")) or "CRITICAL"
    start_line = secret.get("StartLine", 0)
    end_line = secret.get("EndLine", start_line)
    raw_match = _text(secret.get("Match"))

    redacted_preview = _redact_secret_text(raw_match)

    lines = [
        f"# Trivy Secret Detection: {title}",
        f"**Severity:** {severity}",
        f"**Category:** {category}",
        f"**Target Artifact:** `{artifact_name}`",
        f"**File Location:** `{target_name}` (Lines {start_line}-{end_line})",
        f"**Rule ID:** `{rule_id}`",
        f"**Redacted Value:** `{redacted_preview}`",
        (
            "\n> [!CAUTION]\n> Secret credentials must be rotated immediately "
            "and purged from source history."
        ),
    ]

    unique_key = _sanitize_id(f"{artifact_name}:{target_name}:{rule_id}:{start_line}")
    return {
        "id": f"trivy:secret:{unique_key}",
        "title": f"Trivy Secret Alert: {title} in {target_name}",
        "content": "\n".join(lines),
        "url": "",
        "_deleted": False,
    }


def render_summary(
    artifact_name: str,
    artifact_type: str,
    metadata: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Render an overarching scan posture summary document for an artifact."""
    vuln_counts: dict[str, int] = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    total_misconfigs = 0
    total_secrets = 0

    for result in results:
        for vuln in result.get("Vulnerabilities") or []:
            sev = _text(vuln.get("Severity")).upper()
            if sev in vuln_counts:
                vuln_counts[sev] += 1
            else:
                vuln_counts.setdefault(sev, 0)
                vuln_counts[sev] += 1

        total_misconfigs += len(result.get("Misconfigurations") or [])
        total_secrets += len(result.get("Secrets") or [])

    os_info = metadata.get("OS") or {}
    os_name = f"{os_info.get('Family', '')} {os_info.get('Name', '')}".strip() or "Unknown"

    lines = [
        f"# Trivy Security Posture: {artifact_name}",
        f"**Artifact Type:** {artifact_type or 'container/repository'}",
        f"**Operating System:** {os_name}",
        "\n### Vulnerability Breakdown:",
        f"- **Critical Severity CVEs:** {vuln_counts.get('CRITICAL', 0)}",
        f"- **High Severity CVEs:** {vuln_counts.get('HIGH', 0)}",
        f"- **Medium Severity CVEs:** {vuln_counts.get('MEDIUM', 0)}",
        f"- **Low Severity CVEs:** {vuln_counts.get('LOW', 0)}",
        f"\n**Total Misconfigurations:** {total_misconfigs}",
        f"**Total Leaked Secrets:** {total_secrets}",
    ]

    unique_key = _sanitize_id(artifact_name)
    return {
        "id": f"trivy:summary:{unique_key}",
        "title": f"Trivy Security Posture: {artifact_name}",
        "content": "\n".join(lines),
        "url": "",
        "_deleted": False,
    }


# ---------------------------------------------------------------------------
# Sync & Walk Pipeline
# ---------------------------------------------------------------------------


class _TrivyWalker:
    """Extracts, fingerprints, and manages state for Trivy scan documents."""

    def __init__(
        self,
        reports: list[dict[str, Any]],
        state: dict[str, Any],
        stats: dict[str, int],
        *,
        include: tuple[str, ...],
        check_active: Callable[[], None] | None = None,
    ):
        self.reports = reports
        self.state = state
        self.stats = stats
        self.include = include
        self.check_active = check_active

        for key in ("scanned", "emitted", "skipped", "deleted", "failed"):
            self.stats.setdefault(key, 0)

        self.fingerprints: dict[str, str] = self.state.setdefault("fingerprints", {})
        self.seen: set[str] = set()

    def _check_record(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if self.check_active is not None:
            self.check_active()

        row_id = row["id"]
        self.seen.add(row_id)
        self.stats["scanned"] += 1

        fingerprint = _hash(row)
        if self.fingerprints.get(row_id) == fingerprint:
            self.stats["skipped"] += 1
            return None

        self.fingerprints[row_id] = fingerprint
        self.stats["emitted"] += 1
        return row

    def rows(self) -> Iterator[dict[str, Any]]:
        if self.check_active is not None:
            self.check_active()

        for report in self.reports:
            artifact_name = _text(report.get("ArtifactName")) or "unnamed-artifact"
            artifact_type = _text(report.get("ArtifactType"))
            metadata = report.get("Metadata") or {}
            results = report.get("Results") or []

            # 1. Summary document
            if "summary" in self.include:
                summary_row = render_summary(artifact_name, artifact_type, metadata, results)
                item = self._check_record(summary_row)
                if item is not None:
                    yield item

            # 2. Iterate Results components
            for res in results:
                target_name = _text(res.get("Target")) or artifact_name

                # Vulnerabilities
                if "vulnerabilities" in self.include:
                    for vuln in res.get("Vulnerabilities") or []:
                        row = render_vulnerability(artifact_name, target_name, vuln)
                        item = self._check_record(row)
                        if item is not None:
                            yield item

                # Misconfigurations
                if "misconfigurations" in self.include:
                    for misconfig in res.get("Misconfigurations") or []:
                        row = render_misconfiguration(artifact_name, target_name, misconfig)
                        item = self._check_record(row)
                        if item is not None:
                            yield item

                # Secrets
                if "secrets" in self.include:
                    for secret in res.get("Secrets") or []:
                        row = render_secret(artifact_name, target_name, secret)
                        item = self._check_record(row)
                        if item is not None:
                            yield item

        # 3. Tombstone forgotten / deleted items (e.g. patched CVEs or resolved misconfigs)
        missing_ids = set(self.fingerprints.keys()) - self.seen
        for gone_id in sorted(missing_ids):
            self.fingerprints.pop(gone_id, None)
            self.stats["deleted"] += 1
            yield {"id": gone_id, "_deleted": True}


def _iter_trivy_rows(
    reports: list[dict[str, Any]],
    state: dict[str, Any],
    stats: dict[str, int],
    *,
    include: tuple[str, ...] = KINDS,
    check_active: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    walker = _TrivyWalker(
        reports,
        state,
        stats,
        include=include,
        check_active=check_active,
    )
    yield from walker.rows()


# ---------------------------------------------------------------------------
# dlt Source / Resource Factory
# ---------------------------------------------------------------------------


def _check_resource_name(name: Any) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("resource_name must be a non-empty string")
    if len(name) > 64:
        raise ValueError("resource_name must be at most 64 characters")
    if NamingConvention().normalize_table_identifier(name) != name:
        raise ValueError("resource_name must be lowercase snake_case (letters, digits, _)")


def trivy_source(
    report_path: str | None = None,
    *,
    report_dir: str | None = None,
    json_data: dict[str, Any] | list[dict[str, Any]] | None = None,
    client: TrivyClient | None = None,
    image_name: str | None = None,
    resource_name: str = TRIVY_TABLE_NAME,
    include: tuple[str, ...] = KINDS,
    check_active: Callable[[], None] | None = None,
) -> Any:
    """Return a dlt resource yielding Trivy security audit documents for ``remember``.

    Args:
        report_path: Path to a single Trivy JSON report file.
        report_dir: Directory containing multiple *.json Trivy scan reports.
        json_data: Raw pre-parsed JSON report dict or list of dicts.
        client: Optional TrivyClient for live scan queries against a Trivy server.
        image_name: Image name to scan if using client mode.
        resource_name: Name of the dlt resource/table.
        include: Kinds of findings to sync
            ("vulnerabilities", "misconfigurations", "secrets", "summary").
        check_active: Optional cancellation checkpoint callback.

    Returns:
        A dlt resource configured with primary_key="id" and write_disposition="merge".
    """
    _check_resource_name(resource_name)
    stats: dict[str, int] = {}

    def _load_all_reports() -> list[dict[str, Any]]:
        loaded: list[dict[str, Any]] = []
        if json_data is not None:
            if isinstance(json_data, list):
                loaded.extend(json_data)
            elif isinstance(json_data, dict):
                loaded.append(json_data)

        if report_path and os.path.isfile(report_path):
            loaded.append(TrivyReportParser.load_report(report_path))

        if report_dir and os.path.isdir(report_dir):
            for path in sorted(glob.glob(os.path.join(report_dir, "*.json"))):
                try:
                    loaded.append(TrivyReportParser.load_report(path))
                except TrivyFormatError:
                    continue

        if client and image_name:
            loaded.append(client.scan_image(image_name))

        return loaded

    @dlt.resource(
        name=resource_name,
        primary_key="id",
        write_disposition="merge",
        columns={"_deleted": {"data_type": "bool", "hard_delete": True}},
    )
    def trivy_documents() -> Iterator[dict[str, Any]]:
        stats.clear()
        reports = _load_all_reports()
        yield from _iter_trivy_rows(
            reports,
            dlt.current.resource_state(),
            stats,
            include=include,
            check_active=check_active,
        )

    resource = trivy_documents()
    setattr(resource, DOCUMENT_SOURCE_ATTR, TRIVY_SOURCE_NAME)
    if hasattr(dlt_utils, "PIPELINE_SCOPE_ATTR"):
        setattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR, resource_name)
    resource.cognee_sync_stats = stats
    return resource
