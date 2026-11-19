import pytest

from cognee_community_connector_pubmed.pubmed import (
    PUBMED_SOURCE_NAME,
    fetch_pubmed_articles,
    parse_pubmed_xml,
    pubmed_source,
)

SAMPLE_PUBMED_XML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2024//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_240101.dtd">
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation Status="MEDLINE">
      <PMID>38000001</PMID>
      <Article PubModel="Print-Electronic">
        <Journal>
          <Title>Journal of Clinical Medicine</Title>
          <JournalIssue>
            <PubDate><Year>2024</Year></PubDate>
          </JournalIssue>
        </Journal>
        <ArticleTitle>CRISPR-Cas9 Gene Editing in Oncology Therapeutics</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">CRISPR therapeutics offer targeted disruption of oncogenes.</AbstractText>
          <AbstractText Label="RESULTS">Observed 85% efficacy in target leukemia cell lines.</AbstractText>
          <AbstractText Label="CONCLUSION">High specificity and safety profiles demonstrated.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author>
            <LastName>Doudna</LastName>
            <ForeName>Jennifer</ForeName>
          </Author>
          <Author>
            <LastName>Charpentier</LastName>
            <ForeName>Emmanuelle</ForeName>
          </Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">38000001</ArticleId>
        <ArticleId IdType="doi">10.1016/j.jcm.2024.01.001</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


def test_parse_pubmed_xml():
    articles = parse_pubmed_xml(SAMPLE_PUBMED_XML)
    assert len(articles) == 1
    art = articles[0]
    assert art["pmid"] == "38000001"
    assert art["title"] == "CRISPR-Cas9 Gene Editing in Oncology Therapeutics"
    assert art["authors"] == "Jennifer Doudna, Emmanuelle Charpentier"
    assert art["journal"] == "Journal of Clinical Medicine"
    assert art["year"] == "2024"
    assert art["doi"] == "10.1016/j.jcm.2024.01.001"
    assert "**BACKGROUND:** CRISPR therapeutics offer targeted disruption" in art["content"]
    assert "**RESULTS:** Observed 85% efficacy" in art["content"]


def test_fetch_pubmed_articles_mock():
    def mock_fetch(query, max_res):
        return SAMPLE_PUBMED_XML

    articles = list(fetch_pubmed_articles(query="crispr oncology", max_results=1, fetch_fn=mock_fetch))
    assert len(articles) == 1
    assert articles[0]["pmid"] == "38000001"


def test_pubmed_source_dlt(tmp_path):
    dlt = pytest.importorskip("dlt")

    def mock_fetch(query, max_res):
        return SAMPLE_PUBMED_XML

    db_path = tmp_path / "pubmed.db"
    pipeline = dlt.pipeline(
        pipeline_name="pubmed_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="pubmed_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    source = pubmed_source(query="oncology", fetch_fn=mock_fetch)
    assert getattr(source, "cognee_document_source", None) == PUBMED_SOURCE_NAME

    pipeline.run(source)
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT pmid, title, authors FROM pubmed_articles")
        assert len(rows) == 1
        assert rows[0][0] == "38000001"
        assert "CRISPR-Cas9" in rows[0][1]
