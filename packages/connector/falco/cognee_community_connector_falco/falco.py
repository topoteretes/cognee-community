"""DLT source for Falco runtime security and threat detection.

Ingests Falco JSON alert logs (output_file from falco.yaml) and Falcosidekick HTTP webhook
payloads into structured Cognee documents for agent threat analysis and graph linking.

Features:
- Priority filtering (Emergency down to Debug)
- Namespace, rule name, and MITRE ATT&CK tag filtering
- Command line secret redaction (passwords, bearer tokens, AWS keys, JWTs)
- Workload-level alert grouping with content-hash stability
- Support for cluster and namespace scoped cognee_node_set tagging
- Document-mode registration (DOCUMENT_SOURCE_ATTR = "falco") for entity linking in Cognee
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("falco_connector")
except ImportError:
    logger = logging.getLogger("falco_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

FALCO_SOURCE_NAME = "falco"
FALCO_TABLE_NAME = "falco_alerts"

# Falco priority ranking (0 = highest urgency, 7 = lowest)
FALCO_PRIORITIES: dict[str, int] = {
    "EMERGENCY": 0,
    "ALERT": 1,
    "CRITICAL": 2,
    "ERROR": 3,
    "WARNING": 4,
    "NOTICE": 5,
    "INFORMATIONAL": 6,
    "INFO": 6,
    "DEBUG": 7,
}

# Common MITRE ATT&CK tactic mappings
MITRE_ATTACK_TACTICS: dict[str, str] = {
    "mitre_initial_access": "TA0001: Initial Access",
    "mitre_execution": "TA0002: Execution",
    "mitre_persistence": "TA0003: Persistence",
    "mitre_privilege_escalation": "TA0004: Privilege Escalation",
    "mitre_defense_evasion": "TA0005: Defense Evasion",
    "mitre_credential_access": "TA0006: Credential Access",
    "mitre_discovery": "TA0007: Discovery",
    "mitre_lateral_movement": "TA0008: Lateral Movement",
    "mitre_collection": "TA0009: Collection",
    "mitre_command_and_control": "TA0011: Command and Control",
    "mitre_exfiltration": "TA0010: Exfiltration",
    "mitre_impact": "TA0040: Impact",
}

# Compiled regex patterns for credential & token sanitization in proc.cmdline / proc.args
SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Passwords / tokens in command line flags (--password=secret, -token "xyz", --api-key 123)
    (
        re.compile(
            r"""(?i)(--?(?:password|passwd|pwd|token|api[_-]?key|secret|auth|bearer|access[_-]?key)\s*[=:\s]\s*["']?)([^"'\s&|;]+)(["']?)"""
        ),
        r"\1[REDACTED]\3",
    ),
    # Bearer tokens in headers or shell arguments
    (
        re.compile(r"""(?i)\bbearer\s+[a-zA-Z0-9_\-\.]{12,}\b"""),
        "Bearer [REDACTED]",
    ),
    # AWS Access Keys (AKIA...)
    (
        re.compile(r"""\bAKIA[0-9A-Z]{16}\b"""),
        "AKIA[REDACTED_AWS_KEY]",
    ),
    # AWS Secret Access Keys in env assignments
    (
        re.compile(r"""(?i)(aws_secret_access_key\s*=\s*)[A-Za-z0-9/+=]{30,}"""),
        r"\1[REDACTED_AWS_SECRET]",
    ),
    # GitHub personal / runner tokens
    (
        re.compile(r"""\bgh[pousr]_[A-Za-z0-9_]{30,255}\b"""),
        "[REDACTED_GH_TOKEN]",
    ),
    # Slack / Discord bot tokens
    (
        re.compile(r"""\bxox[baprs]-[0-9A-Za-z\-]{10,}\b"""),
        "[REDACTED_SLACK_TOKEN]",
    ),
    # OpenAI / Anthropic API keys (sk-...)
    (
        re.compile(r"""\bsk-(?:live-)?[a-zA-Z0-9_-]{20,}\b"""),
        "sk-[REDACTED_API_KEY]",
    ),
    # JSON Web Tokens (JWT)
    (
        re.compile(r"""\beyJ[a-zA-Z0-9_\-]{8,}\.[a-zA-Z0-9_\-]{8,}\.[a-zA-Z0-9_\-]+"""),
        "[REDACTED_JWT]",
    ),
]


def redact_sensitive_cmdline(cmdline: str) -> str:
    """Scan and redact passwords, bearer tokens, AWS credentials, and API keys from CLI strings.

    Args:
        cmdline: Raw command line or argument string from proc.cmdline or proc.args.

    Returns:
        Sanitized command string safe for agent indexing and LLM prompt context.
    """
    if not cmdline or not isinstance(cmdline, str):
        return ""
    sanitized = cmdline
    for pattern, replacement in SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def is_priority_eligible(alert_priority: str | None, min_priority: str | None) -> bool:
    """Return True if alert_priority meets or exceeds the minimum severity threshold."""
    if not min_priority:
        return True
    if not alert_priority:
        return False

    min_norm = min_priority.strip().upper()
    alert_norm = alert_priority.strip().upper()

    min_rank = FALCO_PRIORITIES.get(min_norm)
    alert_rank = FALCO_PRIORITIES.get(alert_norm)

    if min_rank is None or alert_rank is None:
        return True  # If unmapped, default to including

    return alert_rank <= min_rank


def infer_workload_name(pod_name: str | None) -> str:
    """Extract higher-level Kubernetes workload name from pod name by stripping pod replica hashes."""
    if not pod_name or not isinstance(pod_name, str):
        return "unknown-workload"

    name = pod_name.strip()
    # Strip Deployment replica set suffix + pod hash: e.g. name-5969566f68-7v2k8 -> name
    name_clean = re.sub(r"-[0-9a-f]{8,10}-[a-z0-9]{5}$", "", name)
    if name_clean != name:
        return name_clean

    # Strip StatefulSet or single hash suffix: e.g. name-7v2k8 -> name
    name_clean = re.sub(r"-[a-z0-9]{5}$", "", name)
    return name_clean


def parse_falco_alerts(
    input_files: str | Path | list[str | Path] | None = None,
    payloads: list[dict[str, Any]] | dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Parse Falco alerts from files, directories, or webhook payloads.

    Supports JSON Lines (falco.yaml output_file), JSON arrays, or in-memory dicts.
    """
    alerts: list[dict[str, Any]] = []

    # 1. Process in-memory payloads
    if payloads is not None:
        if isinstance(payloads, dict):
            alerts.append(payloads)
        elif isinstance(payloads, list):
            alerts.extend(payloads)

    # 2. Process file inputs
    if input_files is not None:
        paths: list[Path] = []
        if isinstance(input_files, (str, Path)):
            p = Path(input_files)
            if p.is_dir():
                paths.extend(sorted(p.glob("*.json")))
                paths.extend(sorted(p.glob("*.log")))
            else:
                paths.append(p)
        elif isinstance(input_files, list):
            for item in input_files:
                p = Path(item)
                if p.is_dir():
                    paths.extend(sorted(p.glob("*.json")))
                    paths.extend(sorted(p.glob("*.log")))
                else:
                    paths.append(p)

        if not paths and not alerts:
            raise FileNotFoundError(f"Falco input path(s) not found: {input_files}")

        for path in paths:
            if not path.exists():
                raise FileNotFoundError(f"Falco log file does not exist: {path}")

            text = path.read_text(encoding="utf-8").strip()
            if not text:
                continue

            # Try parsing as JSON array first
            if text.startswith("["):
                try:
                    data = json.loads(text)
                    if isinstance(data, list):
                        alerts.extend(data)
                        continue
                except json.JSONDecodeError:
                    pass

            # Parse as JSON Lines (standard Falco output)
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    record = json.loads(line)
                    if isinstance(record, dict):
                        alerts.append(record)
                except json.JSONDecodeError as err:
                    logger.warning("Falco: Skipping malformed JSON line in %s: %s", path, err)

    return alerts


def format_falco_alert_markdown(
    group_key: str,
    alerts: list[dict[str, Any]],
    cluster_name: str,
) -> str:
    """Format one or more grouped Falco alerts into a clean, content-hash-stable markdown document."""
    if not alerts:
        return ""

    sample = alerts[0]
    rule_name = sample.get("rule", "Unknown Rule")
    priority = sample.get("priority", "Notice").upper()
    output_msg = sample.get("output", "")

    output_fields = sample.get("output_fields", {})
    k8s_ns = output_fields.get("k8s.ns.name") or sample.get("k8s_namespace") or "default"
    k8s_pod = output_fields.get("k8s.pod.name") or sample.get("k8s_pod") or "unassigned-pod"
    workload = infer_workload_name(k8s_pod)
    container_name = output_fields.get("container.name") or "host"
    container_image = output_fields.get("container.image.repository") or output_fields.get("container.image") or "host"

    proc_name = output_fields.get("proc.name", "unknown")
    proc_pid = output_fields.get("proc.pid", "unknown")
    user_name = output_fields.get("user.name", "unknown")
    user_uid = output_fields.get("user.uid", "unknown")
    fd_name = output_fields.get("fd.name", "")

    raw_cmdline = output_fields.get("proc.cmdline") or output_fields.get("proc.args") or ""
    redacted_cmd = redact_sensitive_cmdline(raw_cmdline)

    tags = sample.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]

    lines = [
        f"# [Falco Runtime Security] {rule_name}",
        "",
        "## Incident & Detection Overview",
        f"- **Rule Name:** `{rule_name}`",
        f"- **Priority Level:** `{priority}`",
        f"- **Cluster:** `{cluster_name}`",
        f"- **Kubernetes Namespace:** `{k8s_ns}`",
        f"- **Workload:** `{workload}`",
        f"- **Pod Target:** `{k8s_pod}`",
        f"- **Container:** `{container_name}` (Image: `{container_image}`)",
        f"- **Detections in Window:** {len(alerts)} alert event(s)",
        "",
        "## Threat Analysis & Runtime Trigger",
        f"- **Detection Message:** {output_msg}",
        f"- **Triggering Process:** `{proc_name}` (PID: `{proc_pid}`)",
        f"- **User Context:** `{user_name}` (UID: `{user_uid}`)",
    ]

    if fd_name:
        lines.append(f"- **Target File / Socket:** `{fd_name}`")

    if redacted_cmd:
        lines.extend([
            "",
            "## Redacted Command Line Execution",
            "```bash",
            redacted_cmd,
            "```",
        ])

    # Format MITRE ATT&CK techniques
    mitre_lines = []
    other_tags = []
    for tag in tags:
        tag_lower = tag.lower()
        if tag_lower in MITRE_ATTACK_TACTICS:
            mitre_lines.append(f"- **Tactic:** {MITRE_ATTACK_TACTICS[tag_lower]} (`{tag}`)")
        elif tag_lower.startswith("mitre_t") or "t1" in tag_lower:
            mitre_lines.append(f"- **Technique:** `{tag}`")
        elif "mitre" in tag_lower:
            mitre_lines.append(f"- **MITRE Tag:** `{tag}`")
        else:
            other_tags.append(tag)

    if mitre_lines:
        lines.extend([
            "",
            "## MITRE ATT&CK Classification",
            *mitre_lines,
        ])

    if other_tags:
        lines.extend([
            "",
            "## Security Rule Tags",
            ", ".join(f"`{t}`" for t in other_tags),
        ])

    lines.extend([
        "",
        "## Recommended Incident Response",
    ])
    if priority in ("EMERGENCY", "ALERT", "CRITICAL"):
        lines.append(
            "- **Immediate Action (Critical/Emergency):** Quarantine pod via NetworkPolicy, capture container memory dump, "
            "and inspect process tree for lateral movement or persistence."
        )
    elif priority in ("ERROR", "WARNING"):
        lines.append(
            "- **Elevated Review (Warning/Error):** Investigate container image vulnerability scan, verify whether process execution "
            "is part of an authorized maintenance job, and review RBAC privileges."
        )
    else:
        lines.append(
            "- **Routine Audit (Notice/Info):** Monitor log frequency and evaluate if rule tuning or pod security standards need adjustment."
        )

    return "\n".join(lines)


def falco_source(
    input_files: str | Path | list[str | Path] | None = None,
    payloads: list[dict[str, Any]] | dict[str, Any] | None = None,
    min_priority: str | None = None,
    namespaces: list[str] | None = None,
    rules: list[str] | None = None,
    tags: list[str] | None = None,
    group_by_workload: bool = True,
    cluster_name: str = "production-cluster",
):
    """Create a dlt source yielding Falco runtime security alerts as searchable Cognee documents.

    Args:
        input_files: File path, directory, or list of paths containing Falco JSON logs.
        payloads: List of parsed Falco alert dicts or single webhook payload.
        min_priority: Minimum severity to ingest (e.g. "Warning", "Critical", "Alert").
        namespaces: Optional list of Kubernetes namespaces to include.
        rules: Optional list of Falco rule names to include.
        tags: Optional list of tags to require (e.g. ["mitre_execution", "container"]).
        group_by_workload: If True, aggregates alerts by (cluster, namespace, workload, rule).
        cluster_name: Kubernetes cluster identifier for entity indexing.

    Returns:
        A dlt source configured for Cognee document ingestion.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The Falco connector requires dlt: pip install "cognee-community-connector-falco" '
            'or pip install "dlt[sqlalchemy]".'
        ) from exc

    raw_alerts = parse_falco_alerts(input_files=input_files, payloads=payloads)

    # Apply filters
    filtered_alerts: list[dict[str, Any]] = []
    ns_set = {n.lower() for n in namespaces} if namespaces else None
    rules_set = {r.lower() for r in rules} if rules else None
    tags_set = {t.lower() for t in tags} if tags else None

    for alert in raw_alerts:
        priority = alert.get("priority")
        if not is_priority_eligible(priority, min_priority):
            continue

        output_fields = alert.get("output_fields", {})
        alert_ns = (output_fields.get("k8s.ns.name") or alert.get("k8s_namespace") or "default").lower()
        if ns_set and alert_ns not in ns_set:
            continue

        rule = (alert.get("rule") or "").lower()
        if rules_set and rule not in rules_set:
            continue

        alert_tags = alert.get("tags", [])
        if isinstance(alert_tags, str):
            alert_tags = [alert_tags]
        alert_tags_lower = {t.lower() for t in alert_tags}
        if tags_set and not (alert_tags_lower & tags_set):
            continue

        filtered_alerts.append(alert)

    logger.info(
        "Falco: Parsed %d alert(s), %d passed active filters.",
        len(raw_alerts),
        len(filtered_alerts),
    )

    # Group alerts if requested
    grouped_documents: list[dict[str, Any]] = []
    if group_by_workload:
        groups: dict[str, list[dict[str, Any]]] = {}
        for alert in filtered_alerts:
            output_fields = alert.get("output_fields", {})
            k8s_ns = output_fields.get("k8s.ns.name") or alert.get("k8s_namespace") or "default"
            k8s_pod = output_fields.get("k8s.pod.name") or alert.get("k8s_pod") or "unassigned-pod"
            workload = infer_workload_name(k8s_pod)
            rule = alert.get("rule") or "unknown_rule"
            key = f"{cluster_name}/{k8s_ns}/{workload}/{rule}".replace(" ", "_")
            groups.setdefault(key, []).append(alert)

        for key, alert_list in groups.items():
            first = alert_list[0]
            output_fields = first.get("output_fields", {})
            k8s_ns = output_fields.get("k8s.ns.name") or first.get("k8s_namespace") or "default"
            rule_name = first.get("rule", "Unknown Rule")
            markdown = format_falco_alert_markdown(key, alert_list, cluster_name)

            doc = {
                "id": key,
                "title": f"[Falco Alert] {rule_name} in {k8s_ns} ({len(alert_list)} events)",
                "url": f"falco://{key}",
                "cluster": cluster_name,
                "namespace": k8s_ns,
                "rule": rule_name,
                "priority": first.get("priority", "Notice"),
                "event_count": len(alert_list),
                "cognee_node_set": [
                    f"cluster:{cluster_name}",
                    f"namespace:{k8s_ns}",
                    "falco",
                    "runtime_security",
                ],
                "content": markdown,
            }
            grouped_documents.append(doc)
    else:
        for idx, alert in enumerate(filtered_alerts):
            uuid_id = alert.get("uuid") or f"{cluster_name}_{alert.get('rule', 'rule')}_{idx}"
            markdown = format_falco_alert_markdown(uuid_id, [alert], cluster_name)
            output_fields = alert.get("output_fields", {})
            k8s_ns = output_fields.get("k8s.ns.name") or alert.get("k8s_namespace") or "default"
            doc = {
                "id": uuid_id,
                "title": f"[Falco Alert] {alert.get('rule')} ({alert.get('priority')})",
                "url": f"falco://{uuid_id}",
                "cluster": cluster_name,
                "namespace": k8s_ns,
                "rule": alert.get("rule", ""),
                "priority": alert.get("priority", "Notice"),
                "event_count": 1,
                "cognee_node_set": [
                    f"cluster:{cluster_name}",
                    f"namespace:{k8s_ns}",
                    "falco",
                    "runtime_security",
                ],
                "content": markdown,
            }
            grouped_documents.append(doc)

    @dlt.resource(name=FALCO_TABLE_NAME, primary_key="id", write_disposition="replace")
    def falco_alerts():
        for doc in grouped_documents:
            yield doc

    @dlt.source(name=FALCO_SOURCE_NAME)
    def _falco():
        return falco_alerts

    source = _falco()
    setattr(source, DOCUMENT_SOURCE_ATTR, FALCO_SOURCE_NAME)
    return source
