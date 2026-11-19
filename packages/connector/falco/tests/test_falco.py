"""Unit tests for the Falco runtime security and threat detection connector."""

from pathlib import Path
import pytest

from cognee_community_connector_falco import (
    FALCO_SOURCE_NAME,
    FALCO_TABLE_NAME,
    falco_source,
    format_falco_alert_markdown,
    infer_workload_name,
    is_priority_eligible,
    parse_falco_alerts,
    redact_sensitive_cmdline,
)


SAMPLE_FALCO_ALERT_TERMINAL_SHELL = {
    "output": "14:22:10.123456789: Notice A shell was spawned in a container with an attached terminal (user=root user_loginuid=0 k8s.ns=production k8s.pod=payment-gateway-6d7b897b77-xyz12 container=payment-svc container_id=e1a2b3c4 image=registry.company.internal/payment:v2.1.0 command=bash -i)",
    "priority": "Critical",
    "rule": "Terminal shell in container",
    "time": "2026-10-25T14:22:10.123456789Z",
    "output_fields": {
        "container.id": "e1a2b3c4d5e6",
        "container.image.repository": "registry.company.internal/payment:v2.1.0",
        "container.name": "payment-svc",
        "evt.time": 1729866130123456789,
        "fd.name": "/dev/pts/1",
        "k8s.ns.name": "production",
        "k8s.pod.name": "payment-gateway-6d7b897b77-xyz12",
        "proc.cmdline": "bash -i --token=super_secret_jwt_token_9999",
        "proc.name": "bash",
        "proc.pid": 4210,
        "user.name": "root",
        "user.uid": 0,
    },
    "tags": ["container", "mitre_execution", "mitre_t1059"],
}

SAMPLE_FALCO_ALERT_SHADOW_READ = {
    "output": "14:23:05.000000000: Warning Sensitive file opened for reading by unexpected process (user=root k8s.ns=production k8s.pod=payment-gateway-6d7b897b77-xyz12 container=payment-svc file=/etc/shadow cmdline=cat /etc/shadow --password admin123)",
    "priority": "Warning",
    "rule": "Read sensitive file untrusted",
    "time": "2026-10-25T14:23:05.000000000Z",
    "output_fields": {
        "container.name": "payment-svc",
        "container.image.repository": "registry.company.internal/payment:v2.1.0",
        "fd.name": "/etc/shadow",
        "k8s.ns.name": "production",
        "k8s.pod.name": "payment-gateway-6d7b897b77-xyz12",
        "proc.cmdline": "cat /etc/shadow --password admin123",
        "proc.name": "cat",
        "proc.pid": 4215,
        "user.name": "root",
        "user.uid": 0,
    },
    "tags": ["filesystem", "mitre_credential_access", "mitre_t1003"],
}

SAMPLE_FALCO_ALERT_DEBUG = {
    "output": "14:25:00.000000000: Debug System informational probe (cmdline=curl http://example.com)",
    "priority": "Debug",
    "rule": "System probe",
    "time": "2026-10-25T14:25:00.000000000Z",
    "output_fields": {
        "k8s.ns.name": "staging",
        "k8s.pod.name": "test-pod-1",
        "proc.cmdline": "curl http://example.com",
        "proc.name": "curl",
    },
    "tags": ["network"],
}


def test_priority_eligibility():
    """Verify priority hierarchy filtering logic."""
    assert is_priority_eligible("Emergency", "Warning") is True
    assert is_priority_eligible("Alert", "Warning") is True
    assert is_priority_eligible("Critical", "Warning") is True
    assert is_priority_eligible("Error", "Warning") is True
    assert is_priority_eligible("Warning", "Warning") is True
    assert is_priority_eligible("Notice", "Warning") is False
    assert is_priority_eligible("Informational", "Warning") is False
    assert is_priority_eligible("Debug", "Warning") is False

    # Case-insensitive
    assert is_priority_eligible("critical", "CRITICAL") is True
    assert is_priority_eligible("WARNING", "critical") is False

    # None filter allows all
    assert is_priority_eligible("Debug", None) is True


def test_secret_redaction():
    """Verify sensitive tokens and passwords are redacted from proc.cmdline."""
    raw_cmd = "python3 worker.py --password=SuperSecretPassword123 --api-key my_secret_api_key_value"
    sanitized = redact_sensitive_cmdline(raw_cmd)
    assert "SuperSecretPassword123" not in sanitized
    assert "my_secret_api_key_value" not in sanitized
    assert "[REDACTED]" in sanitized

    # Direct API key redaction
    direct_cmd = "export OPENAI_API_KEY=sk-ant-api03-1234567890abcdef123456"
    sanitized_direct = redact_sensitive_cmdline(direct_cmd)
    assert "sk-[REDACTED_API_KEY]" in sanitized_direct

    # Standalone AWS Key redaction
    aws_cmd = "my-backup-tool AKIA1234567890ABCDEF s3://bucket"
    sanitized_aws = redact_sensitive_cmdline(aws_cmd)
    assert "AKIA1234567890ABCDEF" not in sanitized_aws
    assert "AKIA[REDACTED_AWS_KEY]" in sanitized_aws

    # Bearer token redaction
    bearer_cmd = "curl -H 'Authorization: Bearer my_jwt_token_string_here_9876' https://api.internal"
    sanitized_bearer = redact_sensitive_cmdline(bearer_cmd)
    assert "my_jwt_token_string_here_9876" not in sanitized_bearer
    assert "Bearer [REDACTED]" in sanitized_bearer


def test_infer_workload_name():
    """Verify extraction of Kubernetes workload name from pod replica names."""
    assert infer_workload_name("payment-gateway-6d7b897b77-xyz12") == "payment-gateway"
    assert infer_workload_name("coredns-554b79b98d-j68lm") == "coredns"
    assert infer_workload_name("redis-worker-0") == "redis-worker-0"
    assert infer_workload_name("standalone-job-abcde") == "standalone-job"
    assert infer_workload_name(None) == "unknown-workload"


def test_parse_falco_alerts(tmp_path: Path):
    """Verify parsing Falco JSON lines log files and JSON arrays."""
    log_file = tmp_path / "falco_alerts.log"
    import json
    lines = [
        json.dumps(SAMPLE_FALCO_ALERT_TERMINAL_SHELL),
        json.dumps(SAMPLE_FALCO_ALERT_SHADOW_READ),
    ]
    log_file.write_text("\n".join(lines), encoding="utf-8")

    alerts = parse_falco_alerts(input_files=log_file)
    assert len(alerts) == 2
    assert alerts[0]["rule"] == "Terminal shell in container"
    assert alerts[1]["rule"] == "Read sensitive file untrusted"


def test_format_falco_alert_markdown():
    """Verify markdown output formatting, MITRE ATT&CK tags, and process details."""
    markdown = format_falco_alert_markdown(
        group_key="prod/payment/shell",
        alerts=[SAMPLE_FALCO_ALERT_TERMINAL_SHELL],
        cluster_name="prod-us-east-1",
    )
    assert "# [Falco Runtime Security] Terminal shell in container" in markdown
    assert "**Cluster:** `prod-us-east-1`" in markdown
    assert "**Kubernetes Namespace:** `production`" in markdown
    assert "**Workload:** `payment-gateway`" in markdown
    assert "## MITRE ATT&CK Classification" in markdown
    assert "TA0002: Execution" in markdown
    assert "## Redacted Command Line Execution" in markdown
    assert "[REDACTED]" in markdown


def test_falco_source_filtering_and_grouping():
    """Verify filtering and workload grouping behavior in dlt source."""
    try:
        import dlt
    except ImportError:
        pytest.skip("dlt not installed in test environment")

    source = falco_source(
        payloads=[
            SAMPLE_FALCO_ALERT_TERMINAL_SHELL,
            SAMPLE_FALCO_ALERT_SHADOW_READ,
            SAMPLE_FALCO_ALERT_DEBUG,
        ],
        min_priority="Warning",
        namespaces=["production"],
        group_by_workload=True,
        cluster_name="k8s-prod-1",
    )

    resources = list(source.resources.values())
    assert len(resources) == 1
    resource = resources[0]
    assert resource.name == FALCO_TABLE_NAME

    items = list(resource)
    # The Debug alert is filtered out by min_priority="Warning" and namespace="staging"
    # Terminal shell (Critical) and Shadow read (Warning) are in production namespace
    # Since they have different rules ("Terminal shell in container" vs "Read sensitive file untrusted"),
    # they form 2 grouped rule documents for workload payment-gateway
    assert len(items) == 2
    rule_names = {item["rule"] for item in items}
    assert "Terminal shell in container" in rule_names
    assert "Read sensitive file untrusted" in rule_names

    # Check cognee_node_set tags
    doc = items[0]
    assert "cluster:k8s-prod-1" in doc["cognee_node_set"]
    assert "namespace:production" in doc["cognee_node_set"]
    assert "runtime_security" in doc["cognee_node_set"]


def test_missing_input_file_error():
    """Verify FileNotFoundError is raised if non-existent file path is specified."""
    with pytest.raises(FileNotFoundError):
        parse_falco_alerts(input_files="/tmp/non_existent_falco_alerts_12345.log")
