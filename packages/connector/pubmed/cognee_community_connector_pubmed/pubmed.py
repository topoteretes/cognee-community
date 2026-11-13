"""PubMed biomedical research data-source connector for cognee.

Queries NCBI E-utilities (esearch.fcgi and efetch.fcgi) and yields
medical literature abstracts and metadata into cognee memory.
"""

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any

import requests
from cognee.shared.logging_utils import get_logger
from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

logger = get_logger("pubmed_connector")

PUBMED_TABLE_NAME = "pubmed_articles"
PUBMED_SOURCE_NAME = "pubmed"

NCBI_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
NCBI_EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def _clean_text(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.strip().split())


def parse_pubmed_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse PubMed ArticleSet XML into structured article dictionaries."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.error("PubMed: XML parse error: %s", exc)
        return []

    articles: list[dict[str, Any]] = []

    for article_elem in root.findall(".//PubmedArticle"):
        medline = article_elem.find("MedlineCitation")
        if medline is None:
            continue

        pmid_elem = medline.find("PMID")
        pmid = pmid_elem.text.strip() if pmid_elem is not None and pmid_elem.text else ""
        if not pmid:
            continue

        article_info = medline.find("Article")
        if article_info is None:
            continue

        title_elem = article_info.find("ArticleTitle")
        title = _clean_text(title_elem.text if title_elem is not None else "Untitled")

        # Abstract
        abstract_chunks: list[str] = []
        abstract_elem = article_info.find("Abstract")
        if abstract_elem is not None:
            for text_node in abstract_elem.findall("AbstractText"):
                label = text_node.get("Label")
                body = _clean_text(text_node.text)
                if body:
                    if label:
                        abstract_chunks.append(f"**{label}:** {body}")
                    else:
                        abstract_chunks.append(body)
        abstract = "\n\n".join(abstract_chunks)

        # Authors
        authors: list[str] = []
        author_list = article_info.find("AuthorList")
        if author_list is not None:
            for author_node in author_list.findall("Author"):
                last = author_node.find("LastName")
                fore = author_node.find("ForeName")
                last_name = last.text.strip() if last is not None and last.text else ""
                fore_name = fore.text.strip() if fore is not None and fore.text else ""
                full = f"{fore_name} {last_name}".strip()
                if full:
                    authors.append(full)
        authors_str = ", ".join(authors)

        # Journal & Date
        journal = ""
        journal_elem = article_info.find("Journal/Title")
        if journal_elem is not None and journal_elem.text:
            journal = journal_elem.text.strip()

        pub_year = ""
        year_elem = article_info.find("Journal/JournalIssue/PubDate/Year")
        if year_elem is not None and year_elem.text:
            pub_year = year_elem.text.strip()

        # DOI
        doi = ""
        article_data = article_elem.find("PubmedData")
        if article_data is not None:
            for aid in article_data.findall("ArticleIdList/ArticleId"):
                if aid.get("IdType") == "doi" and aid.text:
                    doi = aid.text.strip()
                    break

        content = (
            f"# {title}\n\n"
            f"**PMID:** {pmid} | **Journal:** {journal} ({pub_year})\n"
            f"**Authors:** {authors_str}\n"
            f"**DOI:** https://doi.org/{doi}\n\n"
            f"### Abstract\n{abstract}"
        )

        articles.append({
            "id": f"pmid:{pmid}",
            "pmid": pmid,
            "title": title,
            "authors": authors_str,
            "journal": journal,
            "year": pub_year,
            "doi": doi,
            "abstract": abstract,
            "content": content,
        })

    return articles


def fetch_pubmed_articles(
    query: str,
    max_results: int = 20,
    api_key: str | None = None,
    fetch_fn: Any = None,
) -> Iterator[dict[str, Any]]:
    """Execute search on NCBI and fetch PubMed articles."""
    if fetch_fn is not None:
        xml_text = fetch_fn(query, max_results)
        for art in parse_pubmed_xml(xml_text):
            yield art
        return

    # 1. Search for PMIDs
    search_params = {
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "json",
    }
    if api_key:
        search_params["api_key"] = api_key

    try:
        resp = requests.get(NCBI_ESEARCH_URL, params=search_params, timeout=15)
        resp.raise_for_status()
        id_list = resp.json().get("esearchresult", {}).get("idlist", [])
    except Exception as exc:
        logger.error("PubMed: esearch failed: %s", exc)
        return

    if not id_list:
        return

    # 2. Fetch full XML for PMIDs
    fetch_params = {
        "db": "pubmed",
        "id": ",".join(id_list),
        "retmode": "xml",
    }
    if api_key:
        fetch_params["api_key"] = api_key

    try:
        fetch_resp = requests.get(NCBI_EFETCH_URL, params=fetch_params, timeout=20)
        fetch_resp.raise_for_status()
        articles = parse_pubmed_xml(fetch_resp.text)
        for art in articles:
            yield art
    except Exception as exc:
        logger.error("PubMed: efetch failed: %s", exc)


def pubmed_source(
    query: str,
    max_results: int = 20,
    api_key: str | None = None,
    fetch_fn: Any = None,
):
    """Create a dlt source yielding PubMed biomedical articles as documents."""
    import dlt

    @dlt.resource(name=PUBMED_TABLE_NAME, primary_key="id", write_disposition="replace")
    def pubmed_articles():
        count = 0
        for article in fetch_pubmed_articles(
            query=query,
            max_results=max_results,
            api_key=api_key,
            fetch_fn=fetch_fn,
        ):
            count += 1
            yield article
        logger.info("PubMed: synced %d medical article(s).", count)

    @dlt.source(name=PUBMED_SOURCE_NAME)
    def _pubmed():
        return pubmed_articles

    source = _pubmed()
    setattr(source, DOCUMENT_SOURCE_ATTR, PUBMED_SOURCE_NAME)
    return source
