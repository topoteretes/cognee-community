"""Crossref DOI metadata connector for cognee.

Extracts scholarly publication records, author attribution, container venues,
citation counts, and deposited open-access licenses from Crossref (api.crossref.org)
or offline snapshots into clean markdown documents for Cognee graph indexing and agent RAG memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Union

DOCUMENT_SOURCE_ATTR = "crossref"
CROSSREF_API_BASE = "https://api.crossref.org/works"


def normalize_doi(doi: str) -> str:
    """Normalize DOI into clean canonical lowercase identifier."""
    if not doi:
        return ""
    d = doi.strip()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.IGNORECASE)
    d = re.sub(r"^doi:\s*", "", d, flags=re.IGNORECASE)
    return d.lower().strip()


def format_crossref_document(work: Dict[str, Any]) -> str:
    """Render a Crossref publication record into an agent-queryable markdown document."""
    doi = normalize_doi(work.get("DOI") or work.get("doi") or "unknown_doi")
    title_list = work.get("title", [])
    title = title_list[0] if isinstance(title_list, list) and title_list else str(work.get("title") or "Untitled Publication")
    title = title.strip()

    container_list = work.get("container-title", [])
    venue = container_list[0] if isinstance(container_list, list) and container_list else str(work.get("container-title") or "Unknown Venue")
    venue = venue.strip()

    # Format authors
    authors = work.get("author", [])
    author_names = []
    for a in authors:
        given = a.get("given", "").strip()
        family = a.get("family", "").strip()
        full = f"{given} {family}".strip() if given or family else a.get("name", "Unknown Author")
        orcid = a.get("ORCID", "")
        if orcid:
            full = f"[{full}]({orcid})"
        author_names.append(full)
    author_str = ", ".join(author_names) if author_names else "Unknown Authors"

    # Format publication date
    date_parts = (
        work.get("published-print", {}).get("date-parts", [[]])[0]
        or work.get("published-online", {}).get("date-parts", [[]])[0]
        or work.get("issued", {}).get("date-parts", [[]])[0]
        or []
    )
    if len(date_parts) >= 3:
        pub_date = f"{date_parts[0]:04d}-{date_parts[1]:02d}-{date_parts[2]:02d}"
    elif len(date_parts) >= 1:
        pub_date = f"{date_parts[0]:04d}"
    else:
        pub_date = "Unknown Date"

    citations = work.get("is-referenced-by-count", 0)

    # Format deposited licenses
    licenses = work.get("license", [])
    license_urls = [lic.get("URL") for lic in licenses if isinstance(lic, dict) and lic.get("URL")]
    license_str = ", ".join(f"`{u}`" for u in license_urls) if license_urls else "Not deposited / All rights reserved"

    lines = [
        f"# [Crossref Publication] {title}",
        "",
        "## Bibliographic Metadata",
        f"- **Title:** {title}",
        f"- **Canonical DOI:** `{doi}`",
        f"- **Direct Link:** [https://doi.org/{doi}](https://doi.org/{doi})",
        f"- **Authors:** {author_str}",
        f"- **Venue / Container:** `{venue}`",
        f"- **Publication Date:** `{pub_date}`",
        f"- **Citation Count:** {citations:,}",
        f"- **Deposited License:** {license_str}",
    ]

    # Check for update notices (e.g. retractions, errata, updates)
    updates = work.get("update-to", [])
    if updates:
        lines.extend([
            "",
            "## Deposited Update Notices & Relationship Assertions",
        ])
        for upd in updates:
            upd_type = upd.get("type", "update")
            upd_doi = upd.get("DOI", "N/A")
            lines.append(f"- **Notice Type:** `{upd_type}` -> Target DOI: `{upd_doi}`")

    return "\n".join(lines)


def parse_crossref_works(data: Union[Dict[str, Any], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Parse Crossref works payload into structured documents with graph node sets."""
    documents = []

    if isinstance(data, dict):
        if "items" in data:
            works = data["items"]
        elif "message" in data and isinstance(data["message"], dict) and "items" in data["message"]:
            works = data["message"]["items"]
        elif "message" in data and isinstance(data["message"], dict):
            works = [data["message"]]
        elif "DOI" in data or "doi" in data:
            works = [data]
        else:
            works = data.get("works", [])
    elif isinstance(data, list):
        works = data
    else:
        works = []

    for w in works:
        doi = normalize_doi(w.get("DOI") or w.get("doi") or "")
        if not doi:
            continue

        doc_text = format_crossref_document(w)
        content_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()
        doi_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", doi)

        # Build node sets
        node_sets = [f"crossref:doi:{doi_slug}"]

        container_list = w.get("container-title", [])
        venue = container_list[0] if isinstance(container_list, list) and container_list else str(w.get("container-title") or "")
        if venue:
            venue_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", venue.lower().strip())
            node_sets.append(f"crossref:venue:{venue_slug[:32]}")

        for a in w.get("author", []):
            family = a.get("family", "").lower().strip()
            if family:
                node_sets.append(f"crossref:author:{family}")

        documents.append({
            "id": f"crossref_{doi_slug[:32]}",
            "type": "publication",
            "entity_id": doi,
            "text": doc_text,
            "hash": content_hash,
            "node_sets": node_sets,
            "metadata": {
                "source": DOCUMENT_SOURCE_ATTR,
                "doi": doi,
                "venue": venue,
                "citation_count": w.get("is-referenced-by-count", 0),
            },
        })

    return documents


def crossref_source(
    dois: Optional[List[str]] = None,
    mailto: Optional[str] = None,
    snapshot_data: Optional[Union[Dict[str, Any], List[Dict[str, Any]], str, Path]] = None,
) -> Any:
    """Cognee DLT source for indexing Crossref DOI metadata into agent semantic memory."""
    if isinstance(snapshot_data, (str, Path)):
        p = Path(snapshot_data)
        if not p.exists():
            raise FileNotFoundError(f"Crossref snapshot file not found: {p}")
        with open(p, "r", encoding="utf-8") as f:
            raw_payload = json.load(f)
    elif isinstance(snapshot_data, (dict, list)):
        raw_payload = snapshot_data
    elif dois:
        import requests

        user_agent = "Cognee-Crossref-Connector/0.1.0"
        if mailto:
            user_agent += f" (mailto:{mailto})"

        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json",
        }

        collected_works = []
        for raw_doi in dois:
            clean_doi = normalize_doi(raw_doi)
            if not clean_doi:
                continue
            url = f"{CROSSREF_API_BASE}/{clean_doi}"
            resp = requests.get(url, headers=headers, timeout=25)
            if resp.status_code == 200:
                body = resp.json()
                if "message" in body:
                    collected_works.append(body["message"])
            elif resp.status_code == 404:
                continue
            else:
                resp.raise_for_status()

        raw_payload = collected_works
    else:
        raise ValueError("Must provide either 'snapshot_data' or a list of 'dois'.")

    parsed_docs = parse_crossref_works(raw_payload)

    try:
        import dlt
    except ImportError as e:
        raise ImportError(
            "The Crossref connector requires dlt: pip install 'cognee-community-connector-crossref' or pip install 'dlt[sqlalchemy]'."
        ) from e

    @dlt.source(name="crossref")
    def _source() -> Generator[Any, None, None]:
        @dlt.resource(name="publications", write_disposition="replace")
        def _publications() -> Generator[Dict[str, Any], None, None]:
            for doc in parsed_docs:
                yield {
                    "id": doc["id"],
                    "text": doc["text"],
                    "cognee_document_source": DOCUMENT_SOURCE_ATTR,
                    "cognee_node_set": doc.get("node_sets", []),
                    "metadata": doc.get("metadata", {}),
                }

        yield _publications()

    return _source()
