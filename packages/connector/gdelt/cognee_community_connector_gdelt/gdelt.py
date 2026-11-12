"""GDELT DOC 2.0 global news metadata connector for cognee.

Extracts worldwide news events, source domains, and multilingual coverage
from the GDELT DOC 2.0 API (api.gdeltproject.org) or offline snapshots
into clean semantic markdown documents for Cognee graph indexing and agent RAG memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Union
from urllib.parse import quote_plus, urlparse

DOCUMENT_SOURCE_ATTR = "gdelt"
GDELT_DOC_API_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"


def normalize_gdelt_timestamp(ts_str: Optional[str]) -> str:
    """Normalize GDELT timestamp (e.g. 20261024T123000Z or 20261024123000) to ISO string."""
    if not ts_str:
        return "Unknown Date"
    s = ts_str.strip().replace("T", "").replace("Z", "")
    if len(s) >= 14 and s[:14].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]} UTC"
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} UTC"
    return ts_str


def format_article_document(
    article: Dict[str, Any],
    query: str = "",
) -> str:
    """Render a GDELT news article record into an agent-queryable markdown document."""
    title = (article.get("title") or "Untitled Article").strip()
    url = (article.get("url") or article.get("canonical_url") or "https://unknown.source").strip()
    domain = (article.get("domain") or urlparse(url).netloc or "Unknown Domain").lower()
    country = (article.get("sourcecountry") or "Global / Unknown").upper()
    lang = article.get("language") or "English"
    seen_date = normalize_gdelt_timestamp(article.get("seendate") or article.get("date"))

    lines = [
        f"# [GDELT News Article] {title}",
        "",
        "## Article Metadata & Provenance",
        f"- **Headline:** {title}",
        f"- **Publisher Domain:** `{domain}`",
        f"- **Source Country:** `{country}`",
        f"- **Language:** `{lang}`",
        f"- **Publication Date:** `{seen_date}`",
        f"- **Canonical Source URL:** [{domain}]({url})",
    ]

    if query:
        lines.append(f"- **Matching Query:** `{query}`")

    lines.extend([
        "",
        "## Content Notice",
        "> In accordance with copyright and fair use boundaries, GDELT indexes publisher headlines, metadata, and direct source URLs. Full article bodies remain at the primary publisher domain.",
    ])

    return "\n".join(lines)


def parse_gdelt_articles(
    data: Dict[str, Any],
    query: str = "",
) -> List[Dict[str, Any]]:
    """Parse GDELT articles from raw JSON payload into structured documents."""
    documents = []
    articles = data.get("articles", [])
    query_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", query.lower()).strip("_") or "all"

    for art in articles:
        url = (art.get("url") or art.get("canonical_url") or "").strip()
        if not url:
            continue

        domain = (art.get("domain") or urlparse(url).netloc or "domain").lower()
        country = (art.get("sourcecountry") or "global").lower()
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]

        doc_text = format_article_document(art, query=query)
        content_hash = hashlib.sha256(doc_text.encode("utf-8")).hexdigest()

        node_sets = [
            f"gdelt:query:{query_slug}",
            f"gdelt:domain:{domain}",
            f"gdelt:country:{country}",
        ]

        documents.append({
            "id": f"gdelt_{domain}_{url_hash}",
            "type": "news_article",
            "entity_id": url,
            "text": doc_text,
            "hash": content_hash,
            "node_sets": node_sets,
            "metadata": {
                "source": DOCUMENT_SOURCE_ATTR,
                "title": art.get("title"),
                "domain": domain,
                "country": country,
                "language": art.get("language"),
                "url": url,
                "query": query,
            },
        })

    return documents


def gdelt_source(
    query: str = "",
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    max_records: int = 50,
    snapshot_data: Optional[Union[Dict[str, Any], str, Path]] = None,
) -> Any:
    """Cognee DLT source for indexing GDELT global news metadata into agent semantic memory."""
    if isinstance(snapshot_data, (str, Path)):
        p = Path(snapshot_data)
        if not p.exists():
            raise FileNotFoundError(f"GDELT snapshot file not found: {p}")
        with open(p, "r", encoding="utf-8") as f:
            raw_payload = json.load(f)
    elif isinstance(snapshot_data, dict):
        raw_payload = snapshot_data
    elif query:
        import requests

        params: Dict[str, Any] = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": min(max_records, 250),
            "sort": "DateDesc",
        }
        if start_datetime:
            params["startdatetime"] = start_datetime
        if end_datetime:
            params["enddatetime"] = end_datetime

        headers = {
            "User-Agent": "Cognee-GDELT-Connector/0.1.0",
            "Accept": "application/json",
        }

        resp = requests.get(GDELT_DOC_API_ENDPOINT, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        raw_payload = resp.json()
    else:
        raise ValueError("Must provide either 'snapshot_data' or a search 'query'.")

    parsed_docs = parse_gdelt_articles(raw_payload, query=query)

    try:
        import dlt
    except ImportError as e:
        raise ImportError(
            "The GDELT connector requires dlt: pip install 'cognee-community-connector-gdelt' or pip install 'dlt[sqlalchemy]'."
        ) from e

    @dlt.source(name="gdelt")
    def _source() -> Generator[Any, None, None]:
        @dlt.resource(name="news_articles", write_disposition="replace")
        def _articles() -> Generator[Dict[str, Any], None, None]:
            for doc in parsed_docs:
                yield {
                    "id": doc["id"],
                    "text": doc["text"],
                    "cognee_document_source": DOCUMENT_SOURCE_ATTR,
                    "cognee_node_set": doc.get("node_sets", []),
                    "metadata": doc.get("metadata", {}),
                }

        yield _articles()

    return _source()
