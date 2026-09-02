"""Tests for the PubMed connector's query, cursor, deletion, and XML boundaries."""

from cognee_community_connector_pubmed.pubmed import (
    PubMedClient,
    _parse_articles,
    _with_edat_range,
    sync_articles,
)


ARTICLE_XML = b"""<?xml version='1.0'?>
<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article>
<ArticleTitle>A graph RAG article</ArticleTitle><Abstract>
<AbstractText Label='BACKGROUND'>First paragraph.</AbstractText>
<AbstractText>Second paragraph.</AbstractText></Abstract>
<Journal><Title>Journal of Tests</Title><JournalIssue><PubDate>
<Year>2026</Year><Month>Sep</Month><Day>02</Day>
</PubDate></JournalIssue></Journal>
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


class FakePubMedClient:
    def __init__(self, current_ids, changed_ids=()):
        self.current_ids = list(current_ids)
        self.changed_ids = list(changed_ids)
        self.search_terms = []
        self.fetched_ids = []

    def search_ids(self, term):
        self.search_terms.append(term)
        return self.changed_ids if "[edat]" in term else self.current_ids

    def iter_articles_by_ids(self, ids):
        self.fetched_ids.extend(ids)
        for pmid in ids:
            yield {
                "id": pmid,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "title": f"Article {pmid}",
                "content": f"Content {pmid}",
            }


def test_first_sync_fetches_all_and_records_cursor_and_ids():
    client = FakePubMedClient(["2", "1"])
    state = {}

    rows = list(sync_articles(client, "graph rag", state, today="2026/09/02"))

    assert [row["id"] for row in rows] == ["2", "1"]
    assert all(row["_deleted"] is False for row in rows)
    assert state == {"known_ids": ["1", "2"], "last_edat": "2026/09/02"}


def test_incremental_sync_fetches_changed_and_emits_deletion():
    client = FakePubMedClient(["1", "2"], changed_ids=["2"])
    state = {"known_ids": ["1", "2", "3"], "last_edat": "2026/09/01"}

    rows = list(sync_articles(client, "graph rag", state, today="2026/09/02"))

    assert rows[-1] == {"id": "3", "_deleted": True}
    assert client.fetched_ids == ["2"]
    assert client.search_terms[-1] == "(graph rag) AND 2026/09/01:2026/09/02[edat]"
    assert state == {"known_ids": ["1", "2"], "last_edat": "2026/09/02"}


def test_new_id_is_fetched_even_when_it_falls_below_cursor():
    client = FakePubMedClient(["1", "4"], changed_ids=[])
    state = {"known_ids": ["1"], "last_edat": "2026/09/01"}

    rows = list(sync_articles(client, "graph rag", state, today="2026/09/02"))

    assert [row["id"] for row in rows] == ["4"]


def test_no_changes_is_a_noop_but_advances_the_day_cursor():
    client = FakePubMedClient(["1"], changed_ids=[])
    state = {"known_ids": ["1"], "last_edat": "2026/09/01"}

    assert list(sync_articles(client, "graph rag", state, today="2026/09/02")) == []
    assert state["last_edat"] == "2026/09/02"


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
