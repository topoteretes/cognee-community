"""DLT source for CISA Known Exploited Vulnerabilities (KEV) catalog.

Fetches CISA's authoritative feed of actively exploited vulnerabilities in the wild,
validates catalog integrity (count verification, format check, uniqueness), and yields
each vulnerability as a rich, structured markdown document for Cognee's entity extraction
and vector search pipeline.

Declared with document-mode metadata (DOCUMENT_SOURCE_ATTR = "cisa_kev"), routing entries
through cognify rather than relational table schemas.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import time
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("cisa_kev_connector")
except ImportError:
    logger = logging.getLogger("cisa_kev_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

CISA_KEV_FEED_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
)
CISA_KEV_SOURCE_NAME = "cisa_kev"
CISA_KEV_TABLE_NAME = "cisa_kev_vulnerabilities"

_MAX_RETRIES = 3
_RETRY_BACKOFF = 2.0
_CVE_REGEX = re.compile(r"^CVE-\d{4}-\d{4,}$")


def format_vulnerability_markdown(vuln: dict[str, Any], catalog_version: str = "") -> str:
    """Format a CISA KEV entry into a structured, searchable markdown document."""
    cve_id = vuln.get("cveID", "UNKNOWN_CVE")
    vuln_name = vuln.get("vulnerabilityName", "Unknown Vulnerability")
    vendor = vuln.get("vendorProject", "Unknown Vendor")
    product = vuln.get("product", "Unknown Product")
    date_added = vuln.get("dateAdded", "")
    due_date = vuln.get("dueDate", "")
    short_desc = vuln.get("shortDescription", "").strip()
    required_action = vuln.get("requiredAction", "").strip()
    notes = vuln.get("notes", "").strip()
    ransomware = vuln.get("knownRansomwareCampaignUse", "Unknown")
    cwes = vuln.get("cwes", [])
    cwes_str = ", ".join(cwes) if isinstance(cwes, list) and cwes else "None specified"

    lines = [
        f"# [{cve_id}] {vuln_name}",
        "",
        f"- **CVE ID:** {cve_id}",
        f"- **Vendor / Project:** {vendor}",
        f"- **Product:** {product}",
        f"- **Date Added to KEV:** {date_added}",
        f"- **Remediation Due Date:** {due_date}",
        f"- **Known Ransomware Campaign Use:** {ransomware}",
        f"- **Associated CWEs:** {cwes_str}",
    ]
    if catalog_version:
        lines.append(f"- **Catalog Version:** {catalog_version}")

    lines.extend([
        "",
        "## Description",
        short_desc if short_desc else "No short description provided.",
        "",
        "## Required Remediation Action (CISA Directive)",
        required_action if required_action else "Apply vendor updates according to guidance.",
    ])

    if notes:
        lines.extend([
            "",
            "## Notes & Reference Advisories",
            notes,
        ])

    lines.extend([
        "",
        "## Authoritative References",
        f"- [NIST National Vulnerability Database Entry](https://nvd.nist.gov/vuln/detail/{cve_id})",
        f"- [CISA KEV Catalog Overview](https://www.cisa.gov/known-exploited-vulnerabilities-catalog)",
    ])

    return "\n".join(lines)


def fetch_and_validate_kev_catalog(
    feed_url_or_path: str | Path | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Fetch and validate the CISA KEV catalog payload from URL or local file path."""
    source_target = feed_url_or_path or CISA_KEV_FEED_URL

    raw_data: str = ""
    target_path = Path(str(source_target))
    if target_path.exists() and target_path.is_file():
        logger.info("CISA KEV: Reading catalog from local file: %s", target_path)
        raw_data = target_path.read_text(encoding="utf-8")
    else:
        import requests

        url = str(source_target)
        logger.info("CISA KEV: Fetching catalog from URL: %s", url)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; CogneeCisaKevConnector/0.1; "
                "+https://github.com/topoteretes/cognee)"
            ),
            "Accept": "application/json",
        }

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = requests.get(url, headers=headers, timeout=timeout)
                response.raise_for_status()
                raw_data = response.text
                break
            except Exception as exc:
                last_exc = exc
                if attempt == _MAX_RETRIES:
                    raise RuntimeError(
                        f"Failed to fetch CISA KEV catalog after {_MAX_RETRIES} attempts: {exc}"
                    ) from exc
                sleep_time = _RETRY_BACKOFF * attempt
                logger.warning(
                    "CISA KEV: Fetch attempt %d failed (%s); retrying in %.1fs...",
                    attempt,
                    exc,
                    sleep_time,
                )
                time.sleep(sleep_time)

    try:
        catalog = json.loads(raw_data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"CISA KEV catalog response is not valid JSON: {exc}") from exc

    if not isinstance(catalog, dict):
        raise ValueError("CISA KEV catalog root must be a JSON object.")

    # Validate essential schema fields
    for field in ("catalogVersion", "count", "vulnerabilities"):
        if field not in catalog:
            raise ValueError(f"CISA KEV catalog missing required header field '{field}'.")

    vulnerabilities = catalog.get("vulnerabilities")
    if not isinstance(vulnerabilities, list):
        raise ValueError("CISA KEV 'vulnerabilities' field must be a list.")

    declared_count = catalog.get("count")
    actual_count = len(vulnerabilities)

    if actual_count != declared_count:
        raise ValueError(
            f"CISA KEV catalog count mismatch: declared {declared_count}, actual {actual_count}. "
            "Rejecting corrupted/truncated catalog."
        )

    # Validate individual vulnerability entries
    seen_ids: set[str] = set()
    for idx, vuln in enumerate(vulnerabilities):
        if not isinstance(vuln, dict):
            raise ValueError(f"Vulnerability entry at index {idx} is not an object.")
        cve_id = vuln.get("cveID")
        if not cve_id or not isinstance(cve_id, str):
            raise ValueError(f"Vulnerability at index {idx} missing valid 'cveID'.")
        cve_id_clean = cve_id.strip()
        if not _CVE_REGEX.match(cve_id_clean):
            raise ValueError(f"Invalid CVE identifier format: '{cve_id}' at index {idx}.")
        if cve_id_clean in seen_ids:
            raise ValueError(f"Duplicate CVE ID detected in catalog: '{cve_id_clean}'.")
        seen_ids.add(cve_id_clean)

    logger.info(
        "CISA KEV: Successfully validated catalog version '%s' with %d entries.",
        catalog.get("catalogVersion"),
        actual_count,
    )
    return catalog


def cisa_kev_source(
    feed_url_or_path: str | Path | None = None,
    since_date: str | None = None,
    vendor: str | None = None,
    product: str | None = None,
    known_ransomware_only: bool = False,
    timeout: float = 20.0,
):
    """Create a dlt source yielding CISA KEV entries as searchable documents.

    Args:
        feed_url_or_path: URL to CISA KEV JSON feed or local Path. Defaults to official CISA feed.
        since_date: Optional ISO date string (YYYY-MM-DD); only ingest CVEs added on or after.
        vendor: Optional case-insensitive vendor/project name filter.
        product: Optional case-insensitive product name filter.
        known_ransomware_only: If True, only yield CVEs confirmed in ransomware campaigns.
        timeout: Network request timeout in seconds.

    Returns:
        A dlt source configured for Cognee document ingestion.
    """
    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The CISA KEV connector requires dlt: pip install "cognee-community-connector-cisa-kev" '
            'or pip install "dlt[sqlalchemy]".'
        ) from exc

    catalog = fetch_and_validate_kev_catalog(feed_url_or_path=feed_url_or_path, timeout=timeout)
    catalog_version = str(catalog.get("catalogVersion", ""))
    vulnerabilities = catalog.get("vulnerabilities", [])

    @dlt.resource(name=CISA_KEV_TABLE_NAME, primary_key="id", write_disposition="replace")
    def cisa_kev_vulnerabilities():
        vendor_filter = vendor.lower().strip() if vendor else None
        product_filter = product.lower().strip() if product else None
        since_filter = since_date.strip() if since_date else None

        yielded = 0
        for vuln in vulnerabilities:
            cve_id = vuln["cveID"].strip()
            date_added = vuln.get("dateAdded", "")
            vuln_vendor = vuln.get("vendorProject", "")
            vuln_prod = vuln.get("product", "")
            ransomware = vuln.get("knownRansomwareCampaignUse", "")

            # Apply user filters
            if since_filter and date_added and date_added < since_filter:
                continue
            if vendor_filter and vendor_filter not in vuln_vendor.lower():
                continue
            if product_filter and product_filter not in vuln_prod.lower():
                continue
            if known_ransomware_only and ransomware.lower() != "known":
                continue

            content = format_vulnerability_markdown(vuln, catalog_version=catalog_version)
            row = {
                "id": cve_id,
                "cve_id": cve_id,
                "title": f"[{cve_id}] {vuln.get('vulnerabilityName', '')}",
                "url": f"https://nvd.nist.gov/vuln/detail/{cve_id}",
                "vendor_project": vuln_vendor,
                "product": vuln_prod,
                "vulnerability_name": vuln.get("vulnerabilityName", ""),
                "date_added": date_added,
                "due_date": vuln.get("dueDate", ""),
                "known_ransomware_use": ransomware,
                "cwes": vuln.get("cwes", []),
                "catalog_version": catalog_version,
                "content": content,
            }
            yielded += 1
            yield row

        logger.info("CISA KEV: Yielded %d vulnerability document(s).", yielded)

    @dlt.source(name=CISA_KEV_SOURCE_NAME)
    def _cisa_kev():
        return cisa_kev_vulnerabilities

    source = _cisa_kev()
    setattr(source, DOCUMENT_SOURCE_ATTR, CISA_KEV_SOURCE_NAME)
    return source
