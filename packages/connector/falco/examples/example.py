"""Runnable example for the Falco runtime security and threat detection connector.

Demonstrates priority thresholding, command line secret redaction, workload
name inference, MITRE ATT&CK mapping, and structured Cognee document generation.
"""

from pathlib import Path
import tempfile

from cognee_community_connector_falco import (
    falco_source,
    format_falco_alert_markdown,
    infer_workload_name,
    is_priority_eligible,
    parse_falco_alerts,
    redact_sensitive_cmdline,
)

SAMPLE_ALERTS = [
    {
        "output": "15:04:12.100000000: Critical A shell was spawned in a container with an attached terminal (user=root k8s.ns=production k8s.pod=payment-gateway-6d7b897b77-xyz12 container=payment-svc cmdline=bash -i --token=super_secret_jwt_token_9999)",
        "priority": "Critical",
        "rule": "Terminal shell in container",
        "time": "2026-10-25T15:04:12.100000000Z",
        "output_fields": {
            "container.id": "e1a2b3c4d5e6",
            "container.image.repository": "registry.company.internal/payment:v2.1.0",
            "container.name": "payment-svc",
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
    },
    {
        "output": "15:05:01.000000000: Warning Sensitive file opened for reading by unexpected process (user=root k8s.ns=production k8s.pod=payment-gateway-6d7b897b77-xyz12 container=payment-svc file=/etc/shadow cmdline=cat /etc/shadow --password admin123)",
        "priority": "Warning",
        "rule": "Read sensitive file untrusted",
        "time": "2026-10-25T15:05:01.000000000Z",
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
    },
]


def main():
    print("=" * 70)
    print(" Falco Runtime Security Connector for Cognee - Demo")
    print("=" * 70)

    # 1. Test priority eligibility
    print("\n1. Testing Priority Severity Filtering (Threshold = Warning):")
    priorities = ["Emergency", "Critical", "Warning", "Notice", "Debug"]
    for prio in priorities:
        eligible = is_priority_eligible(prio, "Warning")
        status = "INCLUDED (Meets threshold)" if eligible else "FILTERED OUT (Below threshold)"
        print(f"   - Priority: {prio.ljust(14)} -> {status}")

    # 2. Test command line credential redaction
    print("\n2. Testing Process CLI Credential & Secret Redaction:")
    raw_cmd = "bash -i --token=super_secret_jwt_token_9999 --password=MyRootPass123"
    sanitized = redact_sensitive_cmdline(raw_cmd)
    print(f"   Raw CLI:       {raw_cmd}")
    print(f"   Sanitized CLI: {sanitized}")

    # 3. Test Kubernetes workload name inference
    print("\n3. Testing Kubernetes Workload Name Normalization:")
    pods = [
        "payment-gateway-6d7b897b77-xyz12",
        "coredns-554b79b98d-j68lm",
        "redis-worker-0",
    ]
    for pod in pods:
        workload = infer_workload_name(pod)
        print(f"   - Pod: {pod.ljust(35)} -> Workload: {workload}")

    # 4. Render markdown document preview
    print("\n4. Rendered Falco Threat Document Preview:")
    print("-" * 50)
    markdown = format_falco_alert_markdown(
        group_key="prod/payment/shell",
        alerts=[SAMPLE_ALERTS[0]],
        cluster_name="k8s-prod-us-east-1",
    )
    print(markdown)
    print("-" * 50)

    # 5. Initialize DLT source with alerts
    try:
        print("\n5. Initializing DLT source...")
        source = falco_source(
            payloads=SAMPLE_ALERTS,
            min_priority="Warning",
            namespaces=["production"],
            cluster_name="k8s-prod-us-east-1",
        )
        print("   ✓ DLT source initialized successfully!")
        print(f"   ✓ Document mode marker: {getattr(source, 'cognee_document_source', None)}")
    except ImportError as e:
        print(f"   ℹ️  dlt extra skipped in minimal environment: {e}")

    print("\n✅ Falco connector demo completed successfully!")


if __name__ == "__main__":
    main()
