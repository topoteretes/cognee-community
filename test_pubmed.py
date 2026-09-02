"""Tests for the PubMed connector's query and XML boundaries."""

from cognee_community_connector_pubmed.pubmed import PubMedClient, _parse_articles, _with_edat_range


ARTICLE_XML = b"""<?xml version='1.0'?>
<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article>
<ArticleTitle>A graph RAG article</ArticleTitle><Abstract><AbstractText Label='BACKGROUND'>First paragraph.</AbstractText><AbstractText>Second paragraph.</AbstractText></Abstract>
<Journal><Title>Journal of Tests</Title><JournalIssue><PubDate><Year>2026</Year><Month>Sep</Month><Day>02</Day></PubDate></JournalIssue></Journal>
</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""


def test_edat_range_is_only_added_when_requested():
    assert _with_edat_range("graph rag", None, None) == "graph rag"
    assert _with_edat_range("graph rag", "2026/01/01", "2026/01/31") == (
        "(graph rag) AND 2026/01/01:2026/01/31[edat]"
    )


def test_xml_is_normalized_into_a_provenanced_document():
    articles = list(_parse_articles(ARTICLE_XML))
    assert articles == [
        {
            "id": "123",
            "url": "https://pubmed.ncbi.nlm.nih.gov/123/",
            "title": "A graph RAG article",
            "content": "A graph RAG article\n\nFirst paragraph.\nSecond paragraph.",
            "journal": "Journal of Tests",
            "publication_date": "2026-Sep-02",
        }
    ]


def test_client_searches_then_fetches_ids_in_order():
    calls = []

    def request(url, params):
        calls.append((url, params))
        if url.endswith("esearch.fcgi"):
            return b"<eSearchResult><IdList><Id>123</Id></IdList></eSearchResult>"
        return ARTICLE_XML

    client = PubMedClient(email="contributor@example.com", request=request, sleep=lambda _: None)
    assert [article["id"] for article in client.iter_articles("graph rag")] == ["123"]
    assert calls[0][1]["term"] == "graph rag"
    assert calls[1][1]["id"] == "123"
