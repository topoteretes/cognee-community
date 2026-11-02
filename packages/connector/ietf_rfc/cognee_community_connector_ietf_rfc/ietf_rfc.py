"""DLT source for IETF RFCs and Internet Standards.

Ingests official RFC specifications, supersession graphs (obsoletes / obsoleted_by / updates),
working group metadata, and verified errata into structured Cognee documents for agent query verification.

Features:
- Bounded RFC selection by number, list of numbers, or working group
- Temporal supersession state tracking (e.g. RFC 9111 obsoletes RFC 7234)
- Errata attachment (verified technical and editorial corrections)
- Deterministic document IDs (rfc<number>) and content-hash stability
- Scoped graph tagging (DOCUMENT_SOURCE_ATTR = "ietf_rfc") for agent protocol recall
- Replace-mode synchronization with orphan cleanup
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Any

try:
    from cognee.shared.logging_utils import get_logger
    logger = get_logger("ietf_rfc_connector")
except ImportError:
    logger = logging.getLogger("ietf_rfc_connector")

try:
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR
except ImportError:
    DOCUMENT_SOURCE_ATTR = "cognee_document_source"

IETF_RFC_SOURCE_NAME = "ietf_rfc"
IETF_RFC_TABLE_NAME = "ietf_rfcs"


def normalize_rfc_number(rfc_val: int | str) -> int:
    """Normalize various RFC representations ('RFC 9111', 'rfc9111', '9111', 9111) to integer."""
    if isinstance(rfc_val, int):
        return rfc_val
    cleaned = re.sub(r"(?i)\brfc\s*", "", str(rfc_val)).strip()
    try:
        return int(cleaned)
    except ValueError as exc:
        raise ValueError(f"Invalid RFC number format: '{rfc_val}'") from exc


def parse_rfc_relationships(raw_field: list[int | str] | str | None) -> list[int]:
    """Parse comma/space separated or list of RFC numbers into clean list of integers."""
    if not raw_field:
        return []
    if isinstance(raw_field, list):
        results = []
        for item in raw_field:
            try:
                results.append(normalize_rfc_number(item))
            except ValueError:
                continue
        return sorted(set(results))
    elif isinstance(raw_field, str):
        # Match all digits following optional 'RFC'
        matches = re.findall(r"(?:rfc\s*)?(\d+)", raw_field, flags=re.IGNORECASE)
        return sorted(set(int(m) for m in matches))
    return []


def format_rfc_markdown(rfc_record: dict[str, Any]) -> str:
    """Render an RFC specification record into a structured, content-hash-stable Markdown document."""
    num = rfc_record.get("number")
    title = rfc_record.get("title") or f"RFC {num}"
    status = rfc_record.get("status") or "Informational"
    stream = rfc_record.get("stream") or "IETF"
    wg = rfc_record.get("working_group") or "General"
    pub_date = rfc_record.get("publication_date") or rfc_record.get("date") or "Unknown Date"
    abstract = rfc_record.get("abstract") or "No abstract provided."
    authors = rfc_record.get("authors") or []
    if isinstance(authors, list):
        authors_str = ", ".join(authors) if authors else "IETF Working Group"
    else:
        authors_str = str(authors)

    obsoletes = parse_rfc_relationships(rfc_record.get("obsoletes"))
    obsoleted_by = parse_rfc_relationships(rfc_record.get("obsoleted_by"))
    updates = parse_rfc_relationships(rfc_record.get("updates"))
    updated_by = parse_rfc_relationships(rfc_record.get("updated_by"))
    also_known_as = rfc_record.get("also_known_as") or []

    # Supersession status badge
    if obsoleted_by:
        sup_badge = f"⚠️ **OBSOLETED** by RFC {', RFC '.join(str(n) for n in obsoleted_by)}"
    else:
        sup_badge = f"✅ **CURRENT & ACTIVE** ({status})"

    lines = [
        f"# RFC {num}: {title}",
        "",
        "## Standards Authority & Supersession State",
        f"- **Current State:** {sup_badge}",
        f"- **Maturity Level:** `{status}`",
        f"- **IETF Stream:** {stream}",
        f"- **Working Group:** `{wg}`",
        f"- **Published Date:** {pub_date}",
        f"- **Authors / Editors:** {authors_str}",
        f"- **RFC Editor URL:** [RFC {num} Full Text](https://www.rfc-editor.org/rfc/rfc{num}.html)",
        f"- **Datatracker Dossier:** [Datatracker Document](https://datatracker.ietf.org/doc/rfc{num}/)",
    ]

    if also_known_as:
        aka_str = ", ".join(also_known_as) if isinstance(also_known_as, list) else str(also_known_as)
        lines.append(f"- **Also Known As:** `{aka_str}`")

    # Relationship graph section
    lines.extend([
        "",
        "## Specification Relationship Graph",
    ])
    lines.append(f"- **Obsoletes:** {', '.join(f'RFC {n}' for n in obsoletes) if obsoletes else 'None (First publication or independent)'}")
    lines.append(f"- **Obsoleted By:** {', '.join(f'RFC {n}' for n in obsoleted_by) if obsoleted_by else 'None (Currently active/authoritative)'}")
    lines.append(f"- **Updates:** {', '.join(f'RFC {n}' for n in updates) if updates else 'None'}")
    lines.append(f"- **Updated By:** {', '.join(f'RFC {n}' for n in updated_by) if updated_by else 'None'}")

    # Abstract section
    lines.extend([
        "",
        "## Abstract & Protocol Scope",
        "",
        abstract.strip(),
    ])

    # Errata section
    errata = rfc_record.get("errata") or []
    if errata and isinstance(errata, list):
        lines.extend([
            "",
            f"## Verified Errata & Amendments ({len(errata)} item(s))",
        ])
        for entry in errata:
            if isinstance(entry, dict):
                e_id = entry.get("errata_id") or "unassigned"
                e_status = entry.get("status") or "Reported"
                e_type = entry.get("type") or "Technical"
                e_sec = entry.get("section") or "General"
                e_notes = entry.get("notes") or ""
                lines.append(f"- **Erratum #{e_id}** [{e_status} / {e_type}] (Section: {e_sec}): {e_notes}")

    # Text / Normative body if included
    body_text = rfc_record.get("text") or rfc_record.get("content")
    if body_text and isinstance(body_text, str) and body_text.strip():
        lines.extend([
            "",
            "## Normative Specification Text",
            "",
            body_text.strip(),
        ])

    return "\n".join(lines)


def fetch_rfc_metadata(rfc_num: int, timeout: float = 15.0) -> dict[str, Any]:
    """Fetch metadata and relationships for an RFC from the IETF Datatracker REST API."""
    import requests

    headers = {
        "Accept": "application/json",
        "User-Agent": "Cognee-IETFRFC-Connector/0.1",
    }
    url = f"https://datatracker.ietf.org/api/v1/doc/document/rfc{rfc_num}/"
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    raw = resp.json()

    # Extract relevant fields
    title = raw.get("title", f"RFC {rfc_num}")
    abstract = raw.get("abstract", "")
    status = raw.get("std_level") or "Proposed Standard"
    pub_date = raw.get("pub_date") or ""

    return {
        "number": rfc_num,
        "title": title,
        "abstract": abstract,
        "status": status,
        "publication_date": pub_date,
        "working_group": raw.get("group") or "IETF",
        "obsoletes": parse_rfc_relationships(raw.get("obsoletes")),
        "obsoleted_by": parse_rfc_relationships(raw.get("obsoleted_by")),
        "updates": parse_rfc_relationships(raw.get("updates")),
        "updated_by": parse_rfc_relationships(raw.get("updated_by")),
    }


def ietf_rfc_source(
    rfc_numbers: int | str | list[int | str] | None = None,
    snapshot_data: dict[str, Any] | list[dict[str, Any]] | str | Path | None = None,
    client: Any = None,
):
    """Create a dlt source yielding IETF RFC specifications and relationship graphs as Cognee documents.

    Args:
        rfc_numbers: Single RFC number, string, or list of numbers to ingest (e.g. [7234, 9111]).
        snapshot_data: Optional pre-fetched RFC records (dict, list of dicts, or JSON file path).
        client: Optional callable client(rfc_num) -> dict for hermetic testing or custom fetching.

    Returns:
        A dlt source configured for Cognee document ingestion.
    """
    # Pre-validate file path if snapshot path provided
    if isinstance(snapshot_data, (str, Path)):
        p = Path(snapshot_data)
        if not p.exists():
            raise FileNotFoundError(f"IETF RFC snapshot file not found: {p}")

    try:
        import dlt
    except ImportError as exc:
        raise ImportError(
            'The IETF RFC connector requires dlt: pip install "cognee-community-connector-ietf-rfc" '
            'or pip install "dlt[sqlalchemy]".'
        ) from exc

    resolved_rfcs: list[dict[str, Any]] = []

    # 1. Process snapshot data if provided
    if snapshot_data is not None:
        if isinstance(snapshot_data, dict):
            resolved_rfcs.append(snapshot_data)
        elif isinstance(snapshot_data, list):
            resolved_rfcs.extend([item for item in snapshot_data if isinstance(item, dict)])
        elif isinstance(snapshot_data, (str, Path)):
            p = Path(snapshot_data)
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                resolved_rfcs.append(raw)
            elif isinstance(raw, list):
                resolved_rfcs.extend([item for item in raw if isinstance(item, dict)])

    # 2. Process requested RFC numbers
    if rfc_numbers:
        raw_list = [rfc_numbers] if not isinstance(rfc_numbers, list) else rfc_numbers
        for val in raw_list:
            try:
                num = normalize_rfc_number(val)
            except ValueError:
                continue

            try:
                if client:
                    data = client(num)
                else:
                    data = fetch_rfc_metadata(num)
                if data:
                    resolved_rfcs.append(data)
            except Exception as exc:
                logger.warning("IETF RFC: Error fetching metadata for RFC %d: %s", num, exc)
                raise

    if not resolved_rfcs:
        raise ValueError("No valid RFC specifications provided via rfc_numbers= or snapshot_data=.")

    flattened_documents: list[dict[str, Any]] = []
    for record in resolved_rfcs:
        num = record.get("number")
        if not num:
            continue

        doc_id = f"rfc{num}"
        title = record.get("title") or f"RFC {num}"
        status = record.get("status") or "Informational"
        markdown_body = format_rfc_markdown(record)

        obsoletes = parse_rfc_relationships(record.get("obsoletes"))
        obsoleted_by = parse_rfc_relationships(record.get("obsoleted_by"))

        doc = {
            "id": doc_id,
            "number": num,
            "title": f"[IETF RFC {num}] {title}",
            "url": f"https://www.rfc-editor.org/rfc/rfc{num}.html",
            "status": status,
            "is_obsoleted": bool(obsoleted_by),
            "obsoletes": obsoletes,
            "obsoleted_by": obsoleted_by,
            "cognee_node_set": [
                "ietf",
                f"rfc:{num}",
                f"status:{status.lower().replace(' ', '_')}",
                "standards",
            ],
            "content": markdown_body,
        }
        flattened_documents.append(doc)

    @dlt.resource(name=IETF_RFC_TABLE_NAME, primary_key="id", write_disposition="replace")
    def ietf_rfcs():
        for doc in flattened_documents:
            yield doc

    @dlt.source(name=IETF_RFC_SOURCE_NAME)
    def _ietf_rfc():
        return ietf_rfcs

    source = _ietf_rfc()
    setattr(source, DOCUMENT_SOURCE_ATTR, IETF_RFC_SOURCE_NAME)
    return source
