"""DLT source for AbuseIPDB IP reputation and threat intelligence.

Fetches IP reputation scores, attack categories, ISP/ASN metadata, and TOR node status
from AbuseIPDB REST API v2 or local snapshot payloads into searchable Cognee documents.

Filters private/loopback/reserved IPs locally before network requests, maps numeric attack
category IDs to human-readable names, and registers with document-mode metadata
(DOCUMENT_SOURCE_ATTR = "abuseipdb") so entries flow through cognify for entity linking.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from pathlib import Path
import time
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("abuseipdb_connector")
except ImportError:
    logger = logging.getLogger("abuseipdb_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

ABUSEIPDB_API_URL = "https://api.abuseipdb.com/api/v2"
ABUSEIPDB_SOURCE_NAME = "abuseipdb"
ABUSEIPDB_TABLE_NAME = "abuseipdb_reports"

# Complete AbuseIPDB official category mapping (v2)
ABUSE_CATEGORIES: dict[int, str] = {
    1: "DNS Compromise",
    2: "DNS Poisoning",
    3: "Fraud Orders",
    4: "DDoS Attack",
    5: "FTP Brute-Force",
    6: "Ping of Death",
    7: "Phishing",
    8: "Fraud VoIP",
    9: "Open Proxy",
    10: "Web Spam",
    11: "Email Spam",
    12: "Blog Spam",
    13: "VPN IP",
    14: "Port Scan",
    15: "Hacking",
    16: "SQL Injection",
    17: "Spoofing",
    18: "Brute-Force",
    19: "Bad Web Bot",
    20: "Exploited Host",
    21: "Web App Attack",
    22: "SSH",
    23: "IoT Targeted",
}


def is_public_ip(ip_str: str) -> bool:
    """Return True if the IP is a valid public, routable IPv4 or IPv6 address.

    Filters RFC 1918 (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16), loopback (127.0.0.0/8),
    link-local (169.254.0.0/16), CGNAT (100.64.0.0/10), and multicast addresses.
    """
    if not ip_str or not isinstance(ip_str, str):
        return False
    try:
        ip = ipaddress.ip_address(ip_str.strip())
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            return False
        if ip.version == 4:
            if ip in ipaddress.IPv4Network("10.0.0.0/8"):
                return False
            if ip in ipaddress.IPv4Network("172.16.0.0/12"):
                return False
            if ip in ipaddress.IPv4Network("192.168.0.0/16"):
                return False
            if ip in ipaddress.IPv4Network("100.64.0.0/10"):
                return False
        return True
    except ValueError:
        return False


def resolve_category_names(category_ids: list[int | str] | None) -> list[str]:
    """Map numeric AbuseIPDB category IDs to human-readable names."""
    if not category_ids or not isinstance(category_ids, list):
        return []
    resolved: list[str] = []
    for cat in category_ids:
        try:
            cid = int(cat)
            name = ABUSE_CATEGORIES.get(cid, f"Category {cid}")
            resolved.append(name)
        except (ValueError, TypeError):
            continue
    return resolved


def format_ip_reputation_markdown(ip_data: dict[str, Any]) -> str:
    """Format structured AbuseIPDB report data into a searchable markdown document."""
    ip_addr = ip_data.get("ipAddress") or ip_data.get("ip", "UNKNOWN_IP")
    score = ip_data.get("abuseConfidenceScore", 0)
    country = ip_data.get("countryCode") or ip_data.get("country", "Unknown")
    country_name = ip_data.get("countryName", "")
    isp = ip_data.get("isp", "Unknown ISP")
    domain = ip_data.get("domain", "")
    usage_type = ip_data.get("usageType", "Unknown")
    is_tor = bool(ip_data.get("isTor", False))
    total_reports = ip_data.get("totalReports", 0)
    last_reported = ip_data.get("lastReportedAt", "")
    hostnames = ip_data.get("hostnames") or []
    hostnames_str = ", ".join(hostnames) if isinstance(hostnames, list) and hostnames else "None"

    # Resolve categories
    raw_cats = ip_data.get("categories") or ip_data.get("categoryIds") or []
    if not raw_cats and "reports" in ip_data and isinstance(ip_data["reports"], list):
        cat_set = set()
        for r in ip_data["reports"]:
            if isinstance(r, dict):
                for c in r.get("categories", []):
                    cat_set.add(c)
        raw_cats = sorted(cat_set)

    category_names = resolve_category_names(raw_cats)
    categories_str = ", ".join(category_names) if category_names else "General Suspicious Activity"

    country_display = f"{country} ({country_name})" if country_name else country

    lines = [
        f"# [AbuseIPDB] Threat Intelligence: {ip_addr}",
        "",
        f"- **IP Address:** {ip_addr}",
        f"- **Abuse Confidence Score:** {score}%",
        f"- **Country:** {country_display}",
        f"- **ISP:** {isp}",
        f"- **Domain:** {domain if domain else 'N/A'}",
        f"- **Usage Type:** {usage_type}",
        f"- **Tor Exit Node:** {'Yes' if is_tor else 'No'}",
        f"- **Total Abuse Reports:** {total_reports}",
        f"- **Hostnames:** {hostnames_str}",
    ]
    if last_reported:
        lines.append(f"- **Last Reported At:** {last_reported}")

    lines.extend([
        "",
        "## Attack Categories & Threat Classification",
    ])
    if category_names:
        for cat_name in category_names:
            lines.append(f"- {cat_name}")
    else:
        lines.append("- No specific attack category recorded.")

    lines.extend([
        "",
        "## Threat Severity & Recommendations",
    ])
    if score >= 80:
        lines.append(
            f"**CRITICAL SEVERITY (Score: {score}%):** This IP has been actively flagged in {total_reports} "
            f"distinct abuse reports for {categories_str}. Recommended action: block immediately at the border firewall / security group."
        )
    elif score >= 25:
        lines.append(
            f"**MEDIUM SEVERITY (Score: {score}%):** Suspicious behavior detected ({categories_str}). "
            "Recommended action: apply rate-limiting, step-up MFA, or challenge with captcha."
        )
    else:
        lines.append(
            f"**LOW RISK (Score: {score}%):** Minimal reports on file. Monitor access logs."
        )

    lines.extend([
        "",
        "## Reference Links",
        f"- [AbuseIPDB Check Dossier](https://www.abuseipdb.com/check/{ip_addr})",
    ])

    return "\n".join(lines)


def fetch_ip_check(
    ip: str,
    api_key: str,
    max_age_days: int = 30,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Call AbuseIPDB /api/v2/check endpoint."""
    import requests

    headers = {
        "Key": api_key,
        "Accept": "application/json",
        "User-Agent": "Cognee-AbuseIPDB-Connector/0.1",
    }
    params = {
        "ipAddress": ip,
        "maxAgeInDays": max_age_days,
        "verbose": "true",
    }
    url = f"{ABUSEIPDB_API_URL}/check"
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json().get("data", {})
    return data


def abuseipdb_source(
    api_key: str | None = None,
    ips: list[str] | None = None,
    min_confidence: int = 25,
    max_age_days: int = 30,
    snapshot_data: list[dict[str, Any]] | str | Path | None = None,
    client: Any = None,
):
    """Create a dlt source yielding AbuseIPDB IP reputation reports as searchable documents.

    Args:
        api_key: AbuseIPDB API v2 key. Falls back to ABUSEIPDB_API_KEY environment variable.
        ips: List of target IP addresses to check. Private IPs are automatically filtered.
        min_confidence: Minimum abuse confidence score (0-100) to yield.
        max_age_days: Lookback window in days (default 30).
        snapshot_data: Pre-fetched snapshot data (list of dicts or path to JSON) for offline/hermetic use.
        client: Optional pre-built callable client for testing.

    Returns:
        A dlt source configured for Cognee document ingestion.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The AbuseIPDB connector requires dlt: pip install "cognee-community-connector-abuseipdb" '
            'or pip install "dlt[sqlalchemy]".'
        ) from exc

    resolved_records: list[dict[str, Any]] = []

    # 1. Handle snapshot data if provided (offline / test injection)
    if snapshot_data is not None:
        if isinstance(snapshot_data, list):
            resolved_records = snapshot_data
        elif isinstance(snapshot_data, (str, Path)):
            p = Path(snapshot_data)
            if p.exists() and p.is_file():
                raw = json.loads(p.read_text(encoding="utf-8"))
                resolved_records = raw if isinstance(raw, list) else raw.get("data", [])
    elif ips:
        # 2. Live API check path
        resolved_key = api_key or os.environ.get("ABUSEIPDB_API_KEY")
        if not resolved_key and not client:
            raise ValueError(
                "AbuseIPDB API key required: pass api_key= or set ABUSEIPDB_API_KEY env var."
            )

        # Filter out private IPs locally first
        public_ips = [ip.strip() for ip in ips if is_public_ip(ip)]
        logger.info(
            "AbuseIPDB: Checking %d public IP(s) (filtered from %d requested).",
            len(public_ips),
            len(ips),
        )

        for ip in public_ips:
            try:
                if client:
                    data = client(ip)
                else:
                    data = fetch_ip_check(ip, resolved_key, max_age_days=max_age_days)
                if data:
                    resolved_records.append(data)
            except Exception as exc:
                logger.warning("AbuseIPDB: Failed check for IP %s: %s", ip, exc)
                # Re-raise transient/auth errors so partial snapshot does not trigger erroneous orphan cleanup
                raise

    @dlt.resource(name=ABUSEIPDB_TABLE_NAME, primary_key="id", write_disposition="replace")
    def abuseipdb_reports():
        yielded = 0
        for record in resolved_records:
            score = record.get("abuseConfidenceScore", 0)
            if score < min_confidence:
                continue

            ip_addr = record.get("ipAddress") or record.get("ip")
            if not ip_addr:
                continue

            content = format_ip_reputation_markdown(record)
            row = {
                "id": ip_addr,
                "ip": ip_addr,
                "title": f"[AbuseIPDB] {ip_addr} (Confidence: {score}%)",
                "url": f"https://www.abuseipdb.com/check/{ip_addr}",
                "confidence_score": score,
                "country_code": record.get("countryCode", ""),
                "isp": record.get("isp", ""),
                "domain": record.get("domain", ""),
                "is_tor": bool(record.get("isTor", False)),
                "total_reports": record.get("totalReports", 0),
                "content": content,
            }
            yielded += 1
            yield row

        logger.info("AbuseIPDB: Yielded %d IP reputation document(s).", yielded)

    @dlt.source(name=ABUSEIPDB_SOURCE_NAME)
    def _abuseipdb():
        return abuseipdb_reports

    source = _abuseipdb()
    setattr(source, DOCUMENT_SOURCE_ATTR, ABUSEIPDB_SOURCE_NAME)
    return source
