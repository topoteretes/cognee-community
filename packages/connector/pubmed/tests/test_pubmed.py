"""Test suite for the PubMed data-source connector (fully mocked, no network)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from unittest.mock import patch
from xml.sax.saxutils import escape

import pytest
from cognee.tasks.ingestion.dlt_utils import document_source_tag

from cognee_community_connector_pubmed import (
    PUBMED_SOURCE_NAME,
    PUBMED_TABLE_NAME,
    NCBIClient,
    parse_pubmed_xml,
    pubmed_source,
    render_article_markdown,
    sync_pubmed,
)
from cognee_community_connector_pubmed import pubmed as pubmed_module
from cognee_community_connector_pubmed.pubmed import NCBIError

TODAY = date(2026, 10, 8)

# A realistic EFetch record: structured abstract with inline markup, a collective author,
# an author marked invalid, MeSH major topics, and identifiers in both places NCBI puts them.
ARTICLE_XML = """<?xml version="1.0" ?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2025//EN"
  "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_250101.dtd">
<PubmedArticleSet>
<PubmedArticle>
  <MedlineCitation Status="MEDLINE" Owner="NLM">
    <PMID Version="1">38000001</PMID>
    <Article PubModel="Print-Electronic">
      <Journal>
        <ISSN IssnType="Electronic">1546-170X</ISSN>
        <JournalIssue CitedMedium="Internet">
          <Volume>30</Volume>
          <PubDate><Year>2024</Year><Month>Mar</Month><Day>15</Day></PubDate>
        </JournalIssue>
        <Title>Nature medicine</Title>
        <ISOAbbreviation>Nat Med</ISOAbbreviation>
      </Journal>
      <ArticleTitle>Base editing of <i>PCSK9</i> in non-human primates.</ArticleTitle>
      <ELocationID EIdType="doi" ValidYN="Y">10.1038/s41591-024-0001-x</ELocationID>
      <Abstract>
        <AbstractText Label="BACKGROUND" NlmCategory="BACKGROUND">Hypercholesterolemia
          drives cardiovascular disease.</AbstractText>
        <AbstractText Label="METHODS" NlmCategory="METHODS">We delivered an adenine
          base editor with lipid nanoparticles.</AbstractText>
        <AbstractText Label="RESULTS" NlmCategory="RESULTS">LDL cholesterol fell by
          <b>60%</b>.</AbstractText>
        <AbstractText Label="Conclusions and Relevance">Durable editing is feasible.</AbstractText>
      </Abstract>
      <AuthorList CompleteYN="Y">
        <Author ValidYN="Y">
          <LastName>Doudna</LastName><ForeName>Jennifer A</ForeName><Initials>JA</Initials>
          <AffiliationInfo><Affiliation>University of California, Berkeley.</Affiliation>
          </AffiliationInfo>
        </Author>
        <Author ValidYN="Y">
          <LastName>Liu</LastName><Initials>DR</Initials>
          <AffiliationInfo><Affiliation>Broad Institute.</Affiliation></AffiliationInfo>
          <AffiliationInfo><Affiliation>University of California, Berkeley.</Affiliation>
          </AffiliationInfo>
        </Author>
        <Author ValidYN="N"><LastName>Wrong</LastName><ForeName>Name</ForeName></Author>
        <Author ValidYN="Y"><CollectiveName>Gene Editing Consortium</CollectiveName></Author>
      </AuthorList>
      <PublicationTypeList>
        <PublicationType UI="D016428">Journal Article</PublicationType>
        <PublicationType UI="D013485">Research Support, Non-U.S. Gov't</PublicationType>
      </PublicationTypeList>
    </Article>
    <MeshHeadingList>
      <MeshHeading>
        <DescriptorName UI="D000071997" MajorTopicYN="Y">Gene Editing</DescriptorName>
      </MeshHeading>
      <MeshHeading>
        <DescriptorName UI="D000072156" MajorTopicYN="N">PCSK9 Inhibitors</DescriptorName>
        <QualifierName UI="Q000627" MajorTopicYN="N">therapeutic use</QualifierName>
      </MeshHeading>
    </MeshHeadingList>
    <KeywordList Owner="NOTNLM">
      <Keyword MajorTopicYN="N">base editing</Keyword>
      <Keyword MajorTopicYN="N">lipid nanoparticles</Keyword>
    </KeywordList>
  </MedlineCitation>
  <PubmedData>
    <History>
      <PubMedPubDate PubStatus="received"><Year>2023</Year><Month>10</Month><Day>1</Day>
      </PubMedPubDate>
      <PubMedPubDate PubStatus="entrez"><Year>2024</Year><Month>3</Month><Day>16</Day>
        <Hour>1</Hour><Minute>3</Minute></PubMedPubDate>
    </History>
    <PublicationStatus>ppublish</PublicationStatus>
    <ArticleIdList>
      <ArticleId IdType="pubmed">38000001</ArticleId>
      <ArticleId IdType="doi">10.1038/s41591-024-0001-x</ArticleId>
      <ArticleId IdType="pmc">PMC11000001</ArticleId>
    </ArticleIdList>
  </PubmedData>
</PubmedArticle>
<PubmedBookArticle>
  <BookDocument>
    <PMID Version="1">20301295</PMID>
    <Book>
      <BookTitle book="gene">GeneReviews®</BookTitle>
      <PubDate><Year>1993</Year></PubDate>
    </Book>
    <ArticleTitle book="gene" part="cf">Cystic Fibrosis</ArticleTitle>
    <Abstract><AbstractText>Cystic fibrosis affects the lungs.</AbstractText></Abstract>
    <AuthorList Type="authors">
      <Author><LastName>Ong</LastName><ForeName>Thida</ForeName></Author>
    </AuthorList>
  </BookDocument>
  <PubmedBookData>
    <History><PubMedPubDate PubStatus="entrez"><Year>2010</Year><Month>Mar</Month><Day>20</Day>
    </PubMedPubDate></History>
  </PubmedBookData>
</PubmedBookArticle>
<DeleteCitation><PMID Version="1">12345</PMID></DeleteCitation>
</PubmedArticleSet>
"""


# ---------------------------------------------------------------------------
# Fake NCBI E-utilities
# ---------------------------------------------------------------------------


@dataclass
class Paper:
    pmid: str
    edat: date
    title: str = ""
    topics: set[str] = field(default_factory=lambda: {"crispr"})
    retracted: bool = False


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        *,
        text: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload if payload is not None else {})
        self.content = self.text.encode()

    def json(self) -> Any:
        return self._payload if self._payload is not None else json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)


def paper_xml(paper: Paper) -> str:
    retraction = (
        "<CommentsCorrectionsList><CommentsCorrections RefType='RetractionIn'>"
        "<RefSource>Retraction notice</RefSource></CommentsCorrections></CommentsCorrectionsList>"
        if paper.retracted
        else ""
    )
    pub_type = "Retracted Publication" if paper.retracted else "Journal Article"
    return f"""<PubmedArticle><MedlineCitation><PMID>{paper.pmid}</PMID><Article>
      <Journal><JournalIssue><PubDate><Year>{paper.edat.year}</Year></PubDate></JournalIssue>
      <Title>Journal of Tests</Title></Journal>
      <ArticleTitle>{escape(paper.title or f"Paper {paper.pmid}")}</ArticleTitle>
      <Abstract><AbstractText>Findings of {paper.pmid}.</AbstractText></Abstract>
      <PublicationTypeList><PublicationType>{pub_type}</PublicationType></PublicationTypeList>
      </Article>{retraction}</MedlineCitation>
      <PubmedData><History><PubMedPubDate PubStatus="entrez"><Year>{paper.edat.year}</Year>
      <Month>{paper.edat.month}</Month><Day>{paper.edat.day}</Day></PubMedPubDate></History>
      </PubmedData></PubmedArticle>"""


class FakeNCBISession:
    """In-memory E-utilities: ESearch over Entrez dates with the 10k cap, EFetch XML."""

    def __init__(self, papers: list[Paper] | None = None):
        self.papers = {p.pmid: p for p in papers or []}
        self.deleted: set[str] = set()  # PMIDs EFetch reports as DeleteCitation
        self.requests: list[dict[str, Any]] = []
        self.scripted: list[FakeResponse | Exception] = []

    def add(self, *papers: Paper) -> None:
        for paper in papers:
            self.papers[paper.pmid] = paper

    def request(self, method: str, url: str, data: dict[str, Any] | None = None, timeout=None):
        data = dict(data or {})
        self.requests.append({"method": method, "url": url, "data": data})
        if self.scripted:
            scripted = self.scripted.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            return scripted
        if url.endswith("/esearch.fcgi"):
            return self._esearch(data)
        if url.endswith("/efetch.fcgi"):
            return self._efetch(data)
        return FakeResponse(404, {"error": "not found"})

    def _matches(self, term: str, paper: Paper) -> bool:
        if term.endswith(" AND retracted publication[pt]"):
            return paper.retracted and self._matches(term[1:].split(")")[0], paper)
        return term.lower() in paper.topics

    def _esearch(self, data: dict[str, Any]) -> FakeResponse:
        assert data["db"] == "pubmed" and data["datetype"] == "edat"
        start = date(*map(int, data["mindate"].split("/")))
        end = date(*map(int, data["maxdate"].split("/")))
        hits = sorted(
            (
                p.pmid
                for p in self.papers.values()
                if start <= p.edat <= end and self._matches(data["term"], p)
            ),
            key=int,
            reverse=True,
        )
        retmax = min(int(data["retmax"]), pubmed_module.MAX_SEARCH_RESULTS)
        return FakeResponse(
            200,
            {
                "esearchresult": {
                    "count": str(len(hits)),
                    "retmax": str(retmax),
                    "idlist": hits[:retmax],
                }
            },
        )

    def _efetch(self, data: dict[str, Any]) -> FakeResponse:
        assert data["retmode"] == "xml"
        records = []
        for pmid in data["id"].split(","):
            if pmid in self.deleted:
                records.append(f"<DeleteCitation><PMID>{pmid}</PMID></DeleteCitation>")
            elif pmid in self.papers:
                records.append(paper_xml(self.papers[pmid]))
        return FakeResponse(200, text=f"<PubmedArticleSet>{''.join(records)}</PubmedArticleSet>")

    def calls(self, endpoint: str) -> list[dict[str, Any]]:
        return [r["data"] for r in self.requests if r["url"].endswith(endpoint)]

    def fetched_ids(self) -> list[str]:
        return [pmid for call in self.calls("efetch.fcgi") for pmid in call["id"].split(",")]


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("time.sleep") as sleep:
        yield sleep


def run(session: FakeNCBISession, state: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
    kwargs.setdefault("term", "crispr")
    kwargs.setdefault("today", TODAY)
    client = NCBIClient(session, api_key=kwargs.pop("api_key", None))
    return list(sync_pubmed(client, state, **kwargs))


def ids(rows: list[dict[str, Any]], *, deleted: bool | None = None) -> list[str]:
    return [r["id"] for r in rows if deleted is None or bool(r.get("_deleted")) is deleted]


# ---------------------------------------------------------------------------
# XML parsing & rendering
# ---------------------------------------------------------------------------


def test_parser_keeps_structured_abstract_sections():
    articles, _ = parse_pubmed_xml(ARTICLE_XML)
    article = articles[0]

    assert article.title == "Base editing of PCSK9 in non-human primates."
    assert article.abstract == [
        ("Background", "Hypercholesterolemia drives cardiovascular disease."),
        ("Methods", "We delivered an adenine base editor with lipid nanoparticles."),
        ("Results", "LDL cholesterol fell by 60%."),
        ("Conclusions and Relevance", "Durable editing is feasible."),
    ]


def test_parser_extracts_citation_metadata():
    article = parse_pubmed_xml(ARTICLE_XML)[0][0]

    assert article.pmid == "38000001"
    assert article.authors == ["Jennifer A Doudna", "DR Liu", "Gene Editing Consortium"]
    assert article.affiliations == ["University of California, Berkeley.", "Broad Institute."]
    assert article.journal == "Nature medicine"
    assert article.pub_date == "2024 Mar 15"
    assert article.doi == "10.1038/s41591-024-0001-x"
    assert article.pmcid == "PMC11000001"
    assert article.mesh_terms == ["Gene Editing (major topic)", "PCSK9 Inhibitors"]
    assert article.keywords == ["base editing", "lipid nanoparticles"]
    assert article.publication_types == ["Journal Article", "Research Support, Non-U.S. Gov't"]
    assert article.entrez_date == "2024-03-16"
    assert article.url == "https://pubmed.ncbi.nlm.nih.gov/38000001/"
    assert article.retracted is False


def test_parser_handles_book_chapters_and_delete_citations():
    articles, deleted = parse_pubmed_xml(ARTICLE_XML)
    book = articles[1]

    assert deleted == ["12345"]
    assert (book.pmid, book.title, book.journal, book.pub_date) == (
        "20301295",
        "Cystic Fibrosis",
        "GeneReviews®",
        "1993",
    )
    assert book.authors == ["Thida Ong"]
    assert book.entrez_date == "2010-03-20"


def test_parser_falls_back_to_elocation_doi_and_medline_date():
    xml = """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1</PMID><Article>
      <Journal><JournalIssue><PubDate><MedlineDate>2019 Nov-Dec</MedlineDate></PubDate>
      </JournalIssue><ISOAbbreviation>J Test</ISOAbbreviation></Journal>
      <ArticleTitle>T</ArticleTitle><ELocationID EIdType="doi">10.1/x</ELocationID>
      </Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    article = parse_pubmed_xml(xml)[0][0]
    assert (article.doi, article.journal, article.pub_date) == ("10.1/x", "J Test", "2019 Nov-Dec")


def test_parser_flags_retracted_publications():
    xml = f"<PubmedArticleSet>{paper_xml(Paper('7', TODAY, retracted=True))}</PubmedArticleSet>"
    assert parse_pubmed_xml(xml)[0][0].retracted is True


def test_markdown_rendering_matches_the_documented_layout():
    md = render_article_markdown(parse_pubmed_xml(ARTICLE_XML)[0][0])

    assert md.startswith(
        "# Base editing of PCSK9 in non-human primates.\n\n"
        "**PMID:** 38000001 | **DOI:** 10.1038/s41591-024-0001-x | **PMCID:** PMC11000001 | "
        "**Journal:** Nature medicine (2024 Mar 15)\n"
        "**Authors:** Jennifer A Doudna, DR Liu, Gene Editing Consortium\n"
    )
    assert "## Abstract\n### Background\nHypercholesterolemia" in md
    assert "### Conclusions and Relevance\nDurable editing is feasible." in md
    assert "## Medical Subject Headings (MeSH)\n- Gene Editing (major topic)\n- PCSK9" in md
    assert (
        md.index("## Abstract")
        < md.index("## Medical Subject Headings")
        < md.index("## Affiliations")
    )


def test_unlabelled_abstract_renders_without_subheadings():
    article = parse_pubmed_xml(ARTICLE_XML)[0][1]
    md = render_article_markdown(article, include_heading=False)
    assert "## Abstract\nCystic fibrosis affects the lungs." in md
    assert not md.startswith("#")


# ---------------------------------------------------------------------------
# HTTP layer: throttling, backoff, errors
# ---------------------------------------------------------------------------


def test_rate_limit_backoff_honours_retry_after(_no_sleep):
    session = FakeNCBISession([Paper("1", TODAY)])
    session.scripted = [
        FakeResponse(429, {"error": "API rate limit exceeded"}, headers={"Retry-After": "4"})
    ]

    assert ids(run(session, {})) == ["pubmed:1"]
    assert 4.0 in [c.args[0] for c in _no_sleep.call_args_list]


def test_rate_limit_without_retry_after_backs_off_exponentially(_no_sleep):
    session = FakeNCBISession([Paper("1", TODAY)])
    session.scripted = [FakeResponse(429, {"error": "API rate limit exceeded"})] * 2

    NCBIClient(session).esearch("crispr", TODAY, TODAY, 10)

    backoffs = [c.args[0] for c in _no_sleep.call_args_list if c.args[0] >= 1]
    assert backoffs == [1.0, 2.0]


def test_transient_server_and_network_errors_are_retried():
    import requests

    session = FakeNCBISession([Paper("1", TODAY)])
    session.scripted = [FakeResponse(502), requests.exceptions.ConnectionError("reset")]

    assert NCBIClient(session).esearch("crispr", TODAY, TODAY, 10) == (1, ["1"])
    assert len(session.calls("esearch.fcgi")) == 3


def test_retries_give_up_eventually():
    import requests

    session = FakeNCBISession()
    session.scripted = [FakeResponse(503)] * 10

    with pytest.raises(requests.exceptions.HTTPError):
        NCBIClient(session).esearch("crispr", TODAY, TODAY, 10)
    assert len(session.requests) == pubmed_module._MAX_RETRIES


def test_requests_are_throttled_to_ncbi_limits(_no_sleep):
    session = FakeNCBISession()
    client = NCBIClient(session)
    client.esearch("crispr", TODAY, TODAY, 1)
    client.esearch("crispr", TODAY, TODAY, 1)
    waits = [c.args[0] for c in _no_sleep.call_args_list]
    assert waits and 0.3 < max(waits) <= 0.34  # 3 requests/second without a key

    _no_sleep.reset_mock()
    keyed = NCBIClient(session, api_key="k")
    keyed.esearch("crispr", TODAY, TODAY, 1)
    keyed.esearch("crispr", TODAY, TODAY, 1)
    assert 0.0 < max(c.args[0] for c in _no_sleep.call_args_list) <= 0.11  # 10/s with a key


def test_api_key_and_email_travel_in_the_post_body():
    session = FakeNCBISession()
    NCBIClient(session, api_key="secret", email="me@example.org").esearch("x", TODAY, TODAY, 1)

    request = session.requests[0]
    assert request["method"] == "POST"
    assert "secret" not in request["url"]
    assert request["data"]["api_key"] == "secret"
    assert request["data"]["email"] == "me@example.org"
    assert request["data"]["tool"] == "cognee-community-connector-pubmed"


def test_invalid_api_key_raises_a_clear_error():
    session = FakeNCBISession()
    session.scripted = [FakeResponse(400, {"error": "API key invalid"})]
    with pytest.raises(PermissionError, match="NCBI_API_KEY"):
        NCBIClient(session, api_key="bad").esearch("x", TODAY, TODAY, 1)


def test_esearch_and_efetch_error_payloads_raise():
    session = FakeNCBISession()
    session.scripted = [
        FakeResponse(200, {"esearchresult": {"ERROR": "Invalid query"}}),
        FakeResponse(200, text="<eFetchResult><ERROR>Empty id list</ERROR></eFetchResult>"),
    ]
    client = NCBIClient(session)
    with pytest.raises(NCBIError, match="Invalid query"):
        client.esearch("x", TODAY, TODAY, 1)
    with pytest.raises(NCBIError, match="Empty id list"):
        client.efetch(["1"])


def test_search_splits_windows_above_the_esearch_cap(monkeypatch):
    monkeypatch.setattr(pubmed_module, "MAX_SEARCH_RESULTS", 3)
    papers = [Paper(str(i), date(2026, 1, 1 + i)) for i in range(10)]
    session = FakeNCBISession(papers)

    found = NCBIClient(session).search_ids("crispr", date(2026, 1, 1), date(2026, 1, 31))

    assert sorted(found, key=int) == [p.pmid for p in papers]
    assert len(found) == len(set(found))
    assert len(session.calls("esearch.fcgi")) > 1


def test_single_day_above_the_cap_is_listed_partially_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(pubmed_module, "MAX_SEARCH_RESULTS", 2)
    session = FakeNCBISession([Paper(str(i), TODAY) for i in range(5)])

    found = NCBIClient(session).search_ids("crispr", TODAY, TODAY)

    assert len(found) == 2
    assert "Narrow the query" in caplog.text


# ---------------------------------------------------------------------------
# Sync engine: backfill, incremental cursor, no-op
# ---------------------------------------------------------------------------


def test_initial_backfill_fetches_every_match_in_batches():
    papers = [Paper(str(100 + i), date(2026, 9, 1 + i)) for i in range(7)]
    session = FakeNCBISession([*papers, Paper("999", date(2026, 9, 2), topics={"malaria"})])
    state: dict[str, Any] = {}

    rows = run(session, state, batch_size=3)

    assert ids(rows) == [f"pubmed:{p.pmid}" for p in papers]
    assert [len(c["id"].split(",")) for c in session.calls("efetch.fcgi")] == [3, 3, 1]
    assert state["known_ids"] == [p.pmid for p in papers]
    assert state["last_edat"] == "2026/10/08"


def test_rows_carry_document_fields():
    session = FakeNCBISession([Paper("42", date(2026, 9, 3), title="Prime editing in vivo")])

    (row,) = run(session, {})

    assert row["id"] == "pubmed:42"
    assert row["title"] == "Prime editing in vivo"
    assert row["url"] == "https://pubmed.ncbi.nlm.nih.gov/42/"
    assert row["journal"] == "Journal of Tests"
    assert row["entrez_date"] == "2026-09-03"
    assert row["_deleted"] is False
    assert row["content"].startswith("**PMID:** 42")  # cognee adds the title heading itself
    assert "## Abstract\nFindings of 42." in row["content"]


def test_batch_size_is_capped_at_200():
    session = FakeNCBISession([Paper(str(i), date(2026, 9, 1)) for i in range(1, 251)])
    run(session, {}, batch_size=1000)
    assert [len(c["id"].split(",")) for c in session.calls("efetch.fcgi")] == [200, 50]


def test_mindate_and_maxdate_bound_the_search():
    session = FakeNCBISession(
        [Paper("1", date(2026, 1, 5)), Paper("2", date(2026, 3, 5)), Paper("3", date(2026, 6, 5))]
    )
    state: dict[str, Any] = {}

    rows = run(session, state, mindate="2026/02/01", maxdate="2026-04-30")

    assert ids(rows) == ["pubmed:2"]
    first = session.calls("esearch.fcgi")[0]
    assert (first["mindate"], first["maxdate"]) == ("2026/02/01", "2026/04/30")
    assert state["last_edat"] == "2026/04/30"


def test_incremental_run_searches_from_the_cursor_and_fetches_only_new_pmids():
    session = FakeNCBISession([Paper("1", date(2026, 9, 1)), Paper("2", date(2026, 10, 6))])
    state: dict[str, Any] = {}
    run(session, state, today=date(2026, 10, 6))
    session.requests.clear()

    session.add(Paper("3", date(2026, 10, 7)), Paper("4", date(2026, 10, 8)))
    rows = run(session, state)

    assert ids(rows) == ["pubmed:3", "pubmed:4"]
    window = session.calls("esearch.fcgi")[0]
    # One day of overlap covers records stamped late on the previous US Eastern day.
    assert (window["mindate"], window["maxdate"]) == ("2026/10/05", "2026/10/08")
    assert sorted(session.fetched_ids()) == ["3", "4"]  # PMID 2 is in the window but known
    assert state["last_edat"] == "2026/10/08"
    assert state["known_ids"] == ["1", "2", "3", "4"]


def test_no_op_rerun_fetches_nothing_and_keeps_state():
    session = FakeNCBISession([Paper("1", date(2026, 10, 1))])
    state: dict[str, Any] = {}
    run(session, state)
    snapshot = json.dumps(state, sort_keys=True)
    session.requests.clear()

    assert run(session, state) == []
    assert session.calls("efetch.fcgi") == []
    assert json.dumps(state, sort_keys=True) == snapshot


def test_changing_the_query_rescans_without_refetching_and_forgets_dropped_articles():
    session = FakeNCBISession(
        [
            Paper("1", date(2026, 9, 1), topics={"crispr", "editing"}),
            Paper("2", date(2026, 9, 2), topics={"crispr"}),
            Paper("3", date(2020, 1, 1), topics={"editing"}),
        ]
    )
    state: dict[str, Any] = {}
    run(session, state, term="crispr")
    session.requests.clear()

    rows = run(session, state, term="editing")

    assert ids(rows, deleted=False) == ["pubmed:3"]  # older than the cursor, still found
    assert ids(rows, deleted=True) == ["pubmed:2"]  # no longer selected
    assert session.fetched_ids() == ["3"]  # PMID 1 was already ingested
    assert state["known_ids"] == ["1", "3"]


# ---------------------------------------------------------------------------
# Forget-on-delete & transient-outage guard
# ---------------------------------------------------------------------------


def test_articles_removed_upstream_become_tombstones():
    session = FakeNCBISession([Paper("1", date(2026, 9, 1)), Paper("2", date(2026, 9, 2))])
    state: dict[str, Any] = {}
    run(session, state)

    del session.papers["1"]
    rows = run(session, state)

    assert rows == [{"id": "pubmed:1", "_deleted": True}]
    assert state["known_ids"] == ["2"]


def test_articles_retracted_after_ingestion_are_forgotten():
    session = FakeNCBISession([Paper("1", date(2026, 9, 1)), Paper("2", date(2026, 9, 2))])
    state: dict[str, Any] = {}
    run(session, state)

    session.papers["1"].retracted = True  # the edat does not change on retraction
    rows = run(session, state)

    assert rows == [{"id": "pubmed:1", "_deleted": True}]
    assert state["known_ids"] == ["2"]


def test_retracted_articles_are_not_ingested_unless_asked():
    session = FakeNCBISession([Paper("1", date(2026, 9, 1), retracted=True)])

    assert run(session, {}) == []
    kept = run(
        FakeNCBISession([Paper("1", date(2026, 9, 1), retracted=True)]), {}, drop_retracted=False
    )
    assert ids(kept) == ["pubmed:1"]


def test_delete_citation_in_a_fetched_batch_is_skipped_quietly(caplog):
    """ESearch can lag behind: a PMID is still listed but EFetch says it was deleted."""
    session = FakeNCBISession([Paper("5", date(2026, 9, 1)), Paper("6", date(2026, 9, 2))])
    session.deleted.add("5")
    state: dict[str, Any] = {}

    rows = run(session, state)

    assert ids(rows) == ["pubmed:6"]
    assert state["known_ids"] == ["6"]
    assert "returned nothing" not in caplog.text


def test_pmid_missing_from_efetch_is_not_marked_known(caplog):
    session = FakeNCBISession([Paper("5", date(2026, 9, 1)), Paper("6", date(2026, 9, 2))])
    original = session._efetch

    def drops_five(data):
        return original({**data, "id": data["id"].replace("5,", "")})

    session._efetch = drops_five  # type: ignore[method-assign]
    state: dict[str, Any] = {}
    run(session, state)

    assert state["known_ids"] == ["6"]
    assert "returned nothing for PMIDs ['5']" in caplog.text


def test_empty_listing_while_articles_are_known_skips_the_sweep(caplog):
    session = FakeNCBISession([Paper("1", date(2026, 9, 1)), Paper("2", date(2026, 9, 2))])
    state: dict[str, Any] = {}
    run(session, state)

    session.papers.clear()  # the API "succeeds" but matches nothing
    rows = run(session, state)

    assert rows == []
    assert state["known_ids"] == ["1", "2"]
    assert "skipping the deletion sweep" in caplog.text


def test_api_failure_aborts_without_deleting_or_moving_state():
    session = FakeNCBISession([Paper("1", date(2026, 9, 1))])
    state: dict[str, Any] = {}
    run(session, state, today=date(2026, 9, 30))
    before = json.dumps(state, sort_keys=True)

    del session.papers["1"]
    session.add(Paper("2", date(2026, 10, 1)))
    session.scripted = [FakeResponse(200, {"esearchresult": {"ERROR": "Search backend down"}})]
    emitted: list[dict[str, Any]] = []
    with pytest.raises(NCBIError):
        for row in sync_pubmed(NCBIClient(session), state, term="crispr", today=TODAY):
            emitted.append(row)

    assert emitted == []
    assert json.dumps(state, sort_keys=True) == before


def test_failure_after_rows_were_yielded_leaves_state_untouched():
    session = FakeNCBISession([Paper(str(i), date(2026, 9, i)) for i in range(1, 5)])
    state: dict[str, Any] = {}
    original = session._efetch
    served = 0

    def flaky(data):
        nonlocal served
        served += 1
        return FakeResponse(400, {"error": "bad"}) if served == 2 else original(data)

    session._efetch = flaky  # type: ignore[method-assign]
    with pytest.raises(Exception, match="400"):
        run(session, state, batch_size=2)
    assert state == {}

    session._efetch = original  # type: ignore[method-assign]
    assert len(run(session, state, batch_size=2)) == 4


def test_detect_deletions_off_skips_the_full_listing():
    session = FakeNCBISession([Paper("1", date(2026, 9, 1))])
    state: dict[str, Any] = {}
    run(session, state, drop_retracted=False)
    session.requests.clear()

    del session.papers["1"]
    assert run(session, state, detect_deletions=False, drop_retracted=False) == []
    assert len(session.calls("esearch.fcgi")) == 1  # just the incremental window


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def test_source_declares_document_marker_and_table():
    source = pubmed_source(term="crispr", session=FakeNCBISession())

    assert PUBMED_SOURCE_NAME == "pubmed"
    assert PUBMED_TABLE_NAME == "pubmed_articles"
    assert document_source_tag(source) == "pubmed"
    assert list(source.resources) == ["pubmed_articles"]


def test_source_requires_a_term_and_valid_dates():
    with pytest.raises(ValueError, match="search term"):
        pubmed_source(term="  ", session=FakeNCBISession())
    with pytest.raises(ValueError, match="YYYY/MM/DD"):
        pubmed_source(term="crispr", mindate="last tuesday", session=FakeNCBISession())


def test_api_key_is_optional_and_read_from_the_environment(monkeypatch):
    seen: list[NCBIClient] = []
    original_init = NCBIClient.__init__

    def spy(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        seen.append(self)

    monkeypatch.setattr(NCBIClient, "__init__", spy)
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    pubmed_source(term="crispr", session=FakeNCBISession())
    monkeypatch.setenv("NCBI_API_KEY", " env-key ")
    pubmed_source(term="crispr", session=FakeNCBISession())

    assert [c.api_key for c in seen] == [None, "env-key"]
    assert [c._interval for c in seen] == [0.34, 0.11]


# ---------------------------------------------------------------------------
# End-to-end dlt pipeline (temporary SQLite staging database)
# ---------------------------------------------------------------------------


def _staged(pipeline) -> dict[str, str]:
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, title FROM {PUBMED_TABLE_NAME}") as cursor,
    ):
        return {row[0]: row[1] for row in cursor.fetchall()}


def test_dlt_pipeline_merge_purges_deleted_articles(tmp_path, monkeypatch):
    dlt = pytest.importorskip("dlt")
    monkeypatch.setattr(pubmed_module, "datetime", _FrozenDatetime)
    session = FakeNCBISession(
        [
            Paper("1", date(2026, 9, 1), title="Alpha"),
            Paper("2", date(2026, 9, 2), title="Beta"),
            Paper("3", date(2026, 9, 3), title="Gamma"),
        ]
    )
    pipeline = dlt.pipeline(
        pipeline_name="pubmed_test_pipeline",
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / 'pubmed_staging.db').as_posix()}"
        ),
        dataset_name="pubmed_ds",
        pipelines_dir=str(tmp_path / "dlt_state"),
    )

    pipeline.run(pubmed_source(term="crispr", session=session))
    assert _staged(pipeline) == {"pubmed:1": "Alpha", "pubmed:2": "Beta", "pubmed:3": "Gamma"}

    # Upstream: 1 is deleted, 2 is retracted, 4 is newly indexed.
    del session.papers["1"]
    session.papers["2"].retracted = True
    session.add(Paper("4", TODAY, title="Delta"))
    session.requests.clear()
    pipeline.run(pubmed_source(term="crispr", session=session))

    assert _staged(pipeline) == {"pubmed:3": "Gamma", "pubmed:4": "Delta"}
    # The cursor survived in dlt resource state, so only the recent window was searched.
    assert session.calls("esearch.fcgi")[0]["mindate"] == "2026/10/07"
    assert session.fetched_ids() == ["4"]


class _FrozenDatetime(pubmed_module.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(TODAY.year, TODAY.month, TODAY.day, 12, tzinfo=tz)
